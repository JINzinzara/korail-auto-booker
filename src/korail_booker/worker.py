"""조회부터 발권 확인까지 한 번의 테스트용 구매 흐름을 조정"""

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
        candidate = pick_candidate(trip, self.search(trip))
        if candidate is None:
            return None
        claimed = self.store.claim_candidate(trip_id, candidate)
        if claimed is None:
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

    def reconcile(self, attempt_id: int) -> PurchaseAttempt | None:
        """RECONCILING 구매 시도의 승차권을 확인하고 성공만 TICKETED로 전환"""
        attempt = self.store.get_attempt(attempt_id)
        if attempt is None or attempt.status is not AttemptStatus.RECONCILING:
            return None
        if self.verify_ticket(attempt):
            return self.store.mark_ticketed(attempt_id)
        return attempt
