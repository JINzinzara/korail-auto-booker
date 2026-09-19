"""조회부터 발권 확인까지 한 번의 테스트용 구매 흐름을 조정"""

import time
from collections.abc import Callable, Iterable

from .domain import AttemptStatus, Candidate, PurchaseAttempt, Trip, TripStatus
from .engine import pick_candidate
from .storage import TripStore


class BookingWorker:
    """주입된 mock 작업과 SQLite 상태 전이를 순서대로 실행"""

    def __init__(
        self,
        store: TripStore,
        search: Callable[[Trip], Iterable[Candidate]],
        reserve: Callable[[Candidate], bool],
        pay: Callable[[PurchaseAttempt], None],
        verify_ticket: Callable[[PurchaseAttempt], bool],
    ) -> None:
        """저장소와 offline 조회·예약·결제·확인 작업을 연결"""
        self.store = store
        self.search = search
        self.reserve = reserve
        self.pay = pay
        self.verify_ticket = verify_ticket

    def run_once(self, trip_id: int) -> PurchaseAttempt | None:
        """MONITORING 여행을 한 번 조회하고 구매 흐름을 최대 한 번 실행"""
        trip = self.store.get_trip(trip_id)
        if trip is None or trip.status is not TripStatus.MONITORING:
            return None
        candidates = list(self.search(trip))
        while (candidate := pick_candidate(trip, candidates)) is not None:
            claimed = self.store.claim_candidate(trip_id, candidate)
            if claimed is not None:
                break
            candidates.remove(candidate)
        else:
            return None
        reserving = self.store.start_reservation(claimed.id)
        if reserving is None:
            return None
        if not self.reserve(candidate):
            return self.store.retry_after_reservation_failure(claimed.id)
        reserved = self.store.mark_reserved(claimed.id)
        if reserved is None:
            return None
        paying = self.store.start_payment(claimed.id)
        if paying is None:
            return None
        try:
            self.pay(paying)
        except Exception:
            self.store.mark_reconciling(claimed.id)
            raise
        reconciling = self.store.mark_reconciling(claimed.id)
        if reconciling is None:
            return None
        return self.reconcile(claimed.id)

    def poll(
        self,
        trip_id: int,
        interval_seconds: float = 10,
        *,
        max_polls: int | None = None,
        stop_requested: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> PurchaseAttempt | None:
        """안전한 간격으로 조회하며 발권, 중지 또는 횟수 제한까지 진행"""
        if interval_seconds < 5:
            raise ValueError("poll interval must be at least 5 seconds")
        if max_polls is not None and max_polls < 1:
            raise ValueError("max polls must be positive")

        attempt = self.store.get_active_attempt(trip_id)
        polls = 0
        while max_polls is None or polls < max_polls:
            trip = self.store.get_trip(trip_id)
            if trip is None:
                return None
            if trip.status is TripStatus.MONITORING:
                if stop_requested is not None and stop_requested():
                    self.store.stop_trip(trip_id)
                    return None
                try:
                    attempt = self.run_once(trip_id)
                except Exception:
                    attempt = self.store.get_active_attempt(trip_id)
                    if (
                        attempt is None
                        or attempt.status is not AttemptStatus.RECONCILING
                    ):
                        raise
            elif trip.status is TripStatus.RECONCILING:
                attempt = attempt or self.store.get_active_attempt(trip_id)
                if attempt is None:
                    return None
                attempt = self.reconcile(attempt.id)
            else:
                return attempt

            polls += 1
            trip = self.store.get_trip(trip_id)
            if attempt is not None and attempt.status is AttemptStatus.TICKETED:
                return attempt
            if trip is None or trip.status not in {
                TripStatus.MONITORING,
                TripStatus.RECONCILING,
            }:
                return attempt
            if max_polls is None or polls < max_polls:
                sleep(interval_seconds)
        return attempt

    def reconcile(self, attempt_id: int) -> PurchaseAttempt | None:
        """RECONCILING 구매 시도의 승차권을 확인하고 성공만 TICKETED로 전환"""
        attempt = self.store.get_attempt(attempt_id)
        if attempt is None or attempt.status is not AttemptStatus.RECONCILING:
            return None
        if self.verify_ticket(attempt):
            return self.store.mark_ticketed(attempt_id)
        return attempt
