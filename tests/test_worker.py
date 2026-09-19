"""offline worker의 조회, 선택, claim, 구매 상태 전이 흐름 확인"""

import sys
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import Mock

from korail_booker.domain import AttemptStatus, Candidate, SeatOption, Trip, TripStatus
from korail_booker.storage import TripStore
from korail_booker.worker import BookingWorker


def show_flow(title: str, *lines: str) -> None:
    """테스트의 입력과 실제 산출값을 사람이 읽기 쉽게 출력"""
    print(
        f"\n[{title}]",
        *(f"  {line}" for line in lines),
        sep="\n",
        file=sys.stderr,
        flush=True,
    )


def make_trip() -> Trip:
    """worker 테스트에 사용할 기본 여행 생성"""
    return Trip(
        departure_station="서울",
        arrival_station="부산",
        travel_date=date(2026, 10, 1),
        earliest_departure=time(8),
        latest_departure=time(12),
        train_types=("KTX",),
    )


def make_candidate(train_no: str = "005") -> Candidate:
    """worker가 선택할 전 구간 좌석 후보 생성"""
    return Candidate(
        train_no=train_no,
        train_type="KTX",
        departure_at=datetime(2026, 10, 1, 10),
        arrival_at=datetime(2026, 10, 1, 13),
        seat_option=SeatOption.FULL,
    )


def start_trip(store: TripStore) -> Trip:
    """저장 후 MONITORING 상태가 된 여행 반환"""
    trip = store.create_trip(make_trip())
    store.start_trip(trip.id)
    return store.get_trip(trip.id)


class BookingWorkerTest(unittest.TestCase):
    """worker의 성공, 재시도, 결제 불명확 경로 확인"""

    def test_success_flow(self) -> None:
        """조회부터 승차권 확인까지 성공하면 TICKETED가 되는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            search = Mock(return_value=(make_candidate(),))
            reserve = Mock(return_value=True)
            pay = Mock(return_value=None)
            verify = Mock(return_value=True)
            result = BookingWorker(store, search, reserve, pay, verify).run_once(trip.id)

            show_flow(
                "worker 성공 흐름",
                "입력: 서울 → 부산, KTX 005 FULL",
                "호출: search → reserve → pay → verify_ticket",
                f"출력 구매 상태: {result.status}",
                f"출력 여행 상태: {store.get_trip(trip.id).status}",
            )

            search.assert_called_once_with(trip)
            reserve.assert_called_once_with(make_candidate())
            pay.assert_called_once()
            verify.assert_called_once()
            self.assertEqual(result.status, AttemptStatus.TICKETED)
            self.assertEqual(store.get_trip(trip.id).status, TripStatus.TICKETED)

    def test_reservation_failure_returns_to_monitoring(self) -> None:
        """좌석 확보 실패가 구매 시도를 끝내고 감시를 재개하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            worker = BookingWorker(
                store,
                Mock(return_value=(make_candidate(),)),
                Mock(return_value=False),
                Mock(),
                Mock(),
            )
            result = worker.run_once(trip.id)

            show_flow(
                "예약 실패 재시도",
                "입력: KTX 005 후보의 예약 결과=False",
                f"출력 구매 상태: {result.status}",
                f"출력 여행 상태: {store.get_trip(trip.id).status}",
            )

            self.assertEqual(result.status, AttemptStatus.FAILED)
            self.assertEqual(store.get_trip(trip.id).status, TripStatus.MONITORING)

    def test_payment_error_stays_reconciling(self) -> None:
        """결제 예외가 재결제 대신 RECONCILING으로 남는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            pay = Mock(side_effect=TimeoutError("payment result unknown"))
            worker = BookingWorker(
                store,
                Mock(return_value=(make_candidate(),)),
                Mock(return_value=True),
                pay,
                Mock(return_value=False),
            )

            with self.assertRaises(TimeoutError):
                worker.run_once(trip.id)
            attempt_id = pay.call_args.args[0].id
            attempt = store.get_attempt(attempt_id)

            show_flow(
                "결제 결과 불명확",
                "입력: pay에서 TimeoutError",
                f"출력 구매 상태: {attempt.status}",
                f"출력 여행 상태: {store.get_trip(trip.id).status}",
                "다음 행동: 재결제 없이 승차권 조회",
            )

            self.assertEqual(attempt.status, AttemptStatus.RECONCILING)
            self.assertEqual(store.get_trip(trip.id).status, TripStatus.RECONCILING)

    def test_reconcile_can_retry_ticket_check(self) -> None:
        """승차권 미확인 상태를 유지한 뒤 조회만 재시도하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            verify = Mock(side_effect=(False, True))
            worker = BookingWorker(
                store,
                Mock(return_value=(make_candidate(),)),
                Mock(return_value=True),
                Mock(return_value=None),
                verify,
            )
            first = worker.run_once(trip.id)
            second = worker.reconcile(first.id)

            show_flow(
                "승차권 확인 재시도",
                f"첫 조회=False → {first.status}",
                f"두 번째 조회=True → {second.status}",
                f"결제 호출 횟수: {worker.pay.call_count}",
            )

            self.assertEqual(first.status, AttemptStatus.RECONCILING)
            self.assertEqual(second.status, AttemptStatus.TICKETED)
            self.assertEqual(worker.pay.call_count, 1)
            self.assertEqual(verify.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
