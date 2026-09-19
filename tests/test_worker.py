"""offline worker의 조회, 선택, claim, 구매 상태 전이 흐름 확인"""

import sys
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import Mock

from korail_booker.domain import (
    AttemptStatus,
    Candidate,
    PaymentOutcomeUnknownError,
    Reservation,
    SeatOption,
    Trip,
    TripStatus,
)
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


def make_reservation() -> Reservation:
    """worker 상태 저장과 결제 테스트에 사용할 내부 예약 생성"""
    return Reservation(reference="hidden", amount=59_800, window_no="001")


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
            reserve = Mock(return_value=make_reservation())
            pay = Mock(return_value=True)
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
            reserve.assert_called_once_with(trip, make_candidate())
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
                Mock(return_value=None),
                Mock(),
                Mock(),
            )
            result = worker.run_once(trip.id)

            show_flow(
                "예약 실패 재시도",
                "입력: KTX 005 후보의 예약 결과=None",
                f"출력 구매 상태: {result.status}",
                f"출력 여행 상태: {store.get_trip(trip.id).status}",
            )

            self.assertEqual(result.status, AttemptStatus.FAILED)
            self.assertEqual(store.get_trip(trip.id).status, TripStatus.MONITORING)

    def test_declined_payment_marks_trip_failed(self) -> None:
        """확정된 결제 거절은 재시도하지 않고 여행을 FAILED로 끝내는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            pay = Mock(return_value=False)
            result = BookingWorker(
                store,
                Mock(return_value=(make_candidate(),)),
                Mock(return_value=make_reservation()),
                pay,
                Mock(),
            ).run_once(trip.id)

            show_flow(
                "결제 거절 종료",
                "입력: 결제 결과=False, 미결제 예약 취소 완료",
                f"출력 구매/여행 상태: {result.status} / {store.get_trip(trip.id).status}",
                "재결제 횟수: 0",
            )
            self.assertEqual(result.status, AttemptStatus.FAILED)
            self.assertEqual(store.get_trip(trip.id).status, TripStatus.FAILED)
            pay.assert_called_once()

    def test_payment_error_stays_reconciling(self) -> None:
        """결제 예외가 재결제 대신 RECONCILING으로 남는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            pay = Mock(
                side_effect=PaymentOutcomeUnknownError("payment result unknown")
            )
            worker = BookingWorker(
                store,
                Mock(return_value=(make_candidate(),)),
                Mock(return_value=make_reservation()),
                pay,
                Mock(return_value=False),
            )

            with self.assertRaises(PaymentOutcomeUnknownError):
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
                Mock(return_value=make_reservation()),
                Mock(return_value=True),
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

    def test_poll_waits_for_changed_search_result(self) -> None:
        """빈 조회 뒤 바뀐 좌석 결과를 다시 조회해 발권하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            search = Mock(side_effect=((), (make_candidate(),)))
            sleep = Mock()
            result = BookingWorker(
                store,
                search,
                Mock(return_value=make_reservation()),
                Mock(return_value=True),
                Mock(return_value=True),
            ).poll(trip.id, max_polls=2, sleep=sleep)

            show_flow(
                "변경되는 좌석 polling",
                "1회차 입력: 예약 가능 후보 없음",
                "10초 대기 후 2회차 입력: KTX 005 FULL",
                f"출력 구매/여행 상태: {result.status} / {store.get_trip(trip.id).status}",
            )
            self.assertEqual(search.call_count, 2)
            sleep.assert_called_once_with(10)
            self.assertEqual(result.status, AttemptStatus.TICKETED)

    def test_poll_reconciles_unknown_payment_without_retry(self) -> None:
        """결제 예외 뒤 재결제하지 않고 승차권 확인 polling을 수행하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            pay = Mock(
                side_effect=PaymentOutcomeUnknownError("payment result unknown")
            )
            verify = Mock(return_value=True)
            result = BookingWorker(
                store,
                Mock(return_value=(make_candidate(),)),
                Mock(return_value=make_reservation()),
                pay,
                verify,
            ).poll(trip.id, max_polls=2, sleep=Mock())

            show_flow(
                "결제 후 안전한 polling",
                "1회차: 결제 응답 TimeoutError → RECONCILING",
                "2회차: 승차권 조회만 실행",
                f"출력: {result.status}, 결제 호출={pay.call_count}회",
            )
            self.assertEqual(result.status, AttemptStatus.TICKETED)
            self.assertEqual(pay.call_count, 1)
            self.assertEqual(verify.call_count, 1)

    def test_poll_stops_before_purchase(self) -> None:
        """사용자 중지 요청이 구매 시작 전 여행을 STOPPED로 전환하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            search = Mock()
            result = BookingWorker(store, search, Mock(), Mock(), Mock()).poll(
                trip.id,
                stop_requested=Mock(return_value=True),
            )

            show_flow(
                "polling 사용자 중지",
                "입력: 첫 조회 전 중지 요청=True",
                f"출력 여행 상태: {store.get_trip(trip.id).status}",
                f"KORAIL 조회 횟수: {search.call_count}",
            )
            self.assertIsNone(result)
            self.assertEqual(store.get_trip(trip.id).status, TripStatus.STOPPED)
            search.assert_not_called()

    def test_poll_skips_previously_failed_candidate(self) -> None:
        """예약 실패한 중복 후보를 건너뛰고 다음 열차를 선택하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            first = make_candidate("005")
            second = make_candidate("007")
            reserve = Mock(side_effect=(None, make_reservation()))
            worker = BookingWorker(
                store,
                Mock(return_value=(first, second)),
                reserve,
                Mock(return_value=True),
                Mock(return_value=True),
            )
            result = worker.poll(trip.id, max_polls=2, sleep=Mock())

            show_flow(
                "예약 실패 후보 건너뛰기",
                "1회차: 005 예약 실패 → FAILED",
                "2회차: 중복 005 제외 → 007 선택",
                f"출력: {result.candidate_key}, {result.status}",
            )
            self.assertTrue(result.candidate_key.startswith("007:"))
            self.assertEqual(result.status, AttemptStatus.TICKETED)
            self.assertEqual(reserve.call_count, 2)

    def test_restart_from_reserved_continues_payment(self) -> None:
        """예약 저장 뒤 재시작하면 예약을 반복하지 않고 결제부터 재개하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "booker.sqlite3"
            store = TripStore(path)
            trip = start_trip(store)
            claimed = store.claim_candidate(trip.id, make_candidate())
            store.start_reservation(claimed.id)
            store.mark_reserved(claimed.id, make_reservation())

            pay = Mock(return_value=True)
            verify = Mock(return_value=True)
            worker = BookingWorker(TripStore(path), Mock(), Mock(), pay, verify)
            result = worker.poll(trip.id, max_polls=1)

            show_flow(
                "RESERVED 재시작 복구",
                "저장 상태: 예약 식별값·운임 보존, 결제 미시도",
                "복구 흐름: 예약 반복 없이 결제 → 승차권 확인",
                f"출력: {result.status}, 결제 호출={pay.call_count}회",
            )
            self.assertEqual(result.status, AttemptStatus.TICKETED)
            self.assertEqual(pay.call_count, 1)
            self.assertEqual(verify.call_count, 1)

    def test_restart_from_paying_reconciles_without_repayment(self) -> None:
        """결제 시작 뒤 재시작하면 재결제 없이 승차권만 확인하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "booker.sqlite3"
            store = TripStore(path)
            trip = start_trip(store)
            claimed = store.claim_candidate(trip.id, make_candidate())
            store.start_reservation(claimed.id)
            store.mark_reserved(claimed.id, make_reservation())
            store.start_payment(claimed.id)

            pay = Mock()
            verify = Mock(return_value=True)
            worker = BookingWorker(TripStore(path), Mock(), Mock(), pay, verify)
            result = worker.poll(trip.id, max_polls=1)

            show_flow(
                "PAYING 재시작 복구",
                "저장 상태: 결제 호출 시각 있음, 결과 불명확",
                "복구 흐름: 재결제 없이 승차권 조회만 실행",
                f"출력: {result.status}, 결제 호출={pay.call_count}회",
            )
            self.assertEqual(result.status, AttemptStatus.TICKETED)
            pay.assert_not_called()
            verify.assert_called_once()

    def test_poll_does_not_retry_search_error(self) -> None:
        """KORAIL 조회 오류를 반복 호출하지 않고 즉시 전달하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = start_trip(store)
            search = Mock(side_effect=RuntimeError("KORAIL rejected request"))
            sleep = Mock()
            worker = BookingWorker(store, search, Mock(), Mock(), Mock())

            with self.assertRaises(RuntimeError):
                worker.poll(trip.id, max_polls=3, sleep=sleep)

            show_flow(
                "조회 오류 시 polling 중단",
                "입력: KORAIL 조회 오류",
                f"출력: 오류 전달, 조회={search.call_count}회",
                f"재시도 대기 횟수: {sleep.call_count}",
            )
            self.assertEqual(search.call_count, 1)
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
