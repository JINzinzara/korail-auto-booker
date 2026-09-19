"""engine의 domain, 후보 선택, SQLite claim 흐름 확인"""

import sys
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

from korail_booker.domain import (
    AttemptStatus,
    Candidate,
    Reservation,
    SeatOption,
    Trip,
    TripStatus,
)
from korail_booker.engine import pick_candidate
from korail_booker.storage import TripStore


def show_flow(title: str, *lines: str) -> None:
    """테스트의 입력과 실제 산출값을 사람이 읽기 쉽게 출력"""
    print(
        f"\n[{title}]",
        *(f"  {line}" for line in lines),
        sep="\n",
        file=sys.stderr,
        flush=True,
    )


def make_trip(**changes: object) -> Trip:
    """테스트 기본값과 선택적 변경값으로 여행(조회) 생성"""
    values = {
        "departure_station": "서울",
        "arrival_station": "부산",
        "travel_date": date(2026, 10, 1),
        "earliest_departure": time(8),
        "latest_departure": time(12),
        "train_types": ("KTX",),
    }
    values.update(changes)
    return Trip(**values)


def make_candidate(
    train_no: str,
    hour: int,
    *,
    train_type: str = "KTX",
    seat_option: SeatOption = SeatOption.FULL,
) -> Candidate:
    """후보 선택과 storage 테스트에 사용할 열차 후보 생성"""
    return Candidate(
        train_no=train_no,
        train_type=train_type,
        departure_at=datetime(2026, 10, 1, hour),
        arrival_at=datetime(2026, 10, 1, hour + 3),
        seat_option=seat_option,
    )


def make_reservation() -> Reservation:
    """재시작 복구 테스트에 사용할 내부 예약 생성"""
    return Reservation(
        reference="hidden",
        amount=59_800,
        window_no="001",
        job_sequence_1="1",
    )


class Phase1Test(unittest.TestCase):
    def test_trip_validation_no_passenger_limit(self) -> None:
        """승객 수의 상한 없이 필수 여행 조건만 검증하여 확인"""
        trip = make_trip(passenger_count=3)
        with self.assertRaises(ValueError) as error:
            make_trip(arrival_station="서울")
        show_flow(
            "여행 조건 검증",
            "입력: 서울 → 부산, 2026-10-01 08:00~12:00, KTX, 승객 3명",
            f"출력: 유효한 Trip, passenger_count={trip.passenger_count}",
            f"잘못된 입력: 서울 → 서울 → {error.exception}",
        )
        self.assertEqual(trip.passenger_count, 3)

    def test_candidate_selection(self) -> None:
        """열차 종류를 필터링하고 전 구간 좌석을 우선 여부 확인"""
        trip = make_trip(allow_merge_seat=True)
        candidates = (
            make_candidate("001", 8, train_type="ITX"),
            make_candidate("003", 9, seat_option=SeatOption.MERGE),
            make_candidate("005", 10),
        )
        selected = pick_candidate(trip, candidates)
        merge_denied = pick_candidate(
            make_trip(), (make_candidate("003", 9, seat_option=SeatOption.MERGE),)
        )
        show_flow(
            "후보 선택",
            "여행 조건: KTX, 08:00~12:00, 좌석·입석 허용",
            "입력 후보: 001 ITX 08:00 FULL / 003 KTX 09:00 MERGE / 005 KTX 10:00 FULL",
            f"출력 후보: {selected.train_no} {selected.train_type} {selected.seat_option}",
            f"좌석·입석 불허 시 003 결과: {merge_denied}",
        )
        self.assertEqual(selected, make_candidate("005", 10))
        self.assertIsNone(merge_denied)

    def test_sqlite_allows(self) -> None:
        """SQLite가 한 여행에 하나의 활성 claim만 허용하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = store.create_trip(make_trip())
            started = store.start_trip(trip.id)
            first_claim = store.claim_candidate(trip.id, make_candidate("001", 9))
            second_claim = store.claim_candidate(trip.id, make_candidate("003", 10))
            stored = store.get_trip(trip.id)
            show_flow(
                "SQLite 단일 claim",
                f"여행 저장: id={trip.id}, status={trip.status}",
                f"감시 시작: {started}",
                f"첫 claim: id={first_claim.id}, status={first_claim.status}",
                f"두 번째 claim: {second_claim}",
                f"최종 여행 상태: {stored.status}",
            )
            self.assertTrue(started)
            self.assertIsNotNone(first_claim)
            self.assertIsNone(second_claim)
            self.assertEqual(stored.status, TripStatus.CLAIMING)

    def test_purchase_state_flow(self) -> None:
        """예약·결제 호출을 한 번만 허용하고 순서대로 상태를 전환하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "booker.sqlite3"
            store = TripStore(path)
            trip = store.create_trip(make_trip())
            store.start_trip(trip.id)
            claimed = store.claim_candidate(trip.id, make_candidate("001", 9))

            early_payment = store.start_payment(claimed.id)
            reserving = store.start_reservation(claimed.id)
            duplicate_reservation = store.start_reservation(claimed.id)
            reservation = make_reservation()
            reserved = store.mark_reserved(claimed.id, reservation)
            store = TripStore(path)
            restored = store.get_attempt(claimed.id)
            paying = store.start_payment(claimed.id)
            duplicate_payment = store.start_payment(claimed.id)
            reconciling = store.mark_reconciling(claimed.id)
            ticketed = store.mark_ticketed(claimed.id)
            final_trip = store.get_trip(trip.id)

            show_flow(
                "구매 상태 전이",
                f"CLAIMED에서 조기 결제: {early_payment}",
                f"예약 시작: {reserving.status}, at={reserving.reserve_attempted_at}",
                f"중복 예약 시작: {duplicate_reservation}",
                f"예약 완료: {reserved.status}",
                "재시작 후 예약 복구: 식별값·운임 저장 확인",
                f"결제 시작: {paying.status}, at={paying.payment_attempted_at}",
                f"중복 결제 시작: {duplicate_payment}",
                f"승차권 확인 대기: {reconciling.status}",
                f"최종 구매/여행: {ticketed.status} / {final_trip.status}",
            )

            self.assertIsNone(early_payment)
            self.assertIsNone(duplicate_reservation)
            self.assertIsNone(duplicate_payment)
            self.assertEqual(reserving.status, AttemptStatus.RESERVING)
            self.assertIsNotNone(reserving.reserve_attempted_at)
            self.assertEqual(reserved.status, AttemptStatus.RESERVED)
            self.assertEqual(restored.reservation, reservation)
            self.assertEqual(paying.status, AttemptStatus.PAYING)
            self.assertIsNotNone(paying.payment_attempted_at)
            self.assertEqual(reconciling.status, AttemptStatus.RECONCILING)
            self.assertEqual(ticketed.status, AttemptStatus.TICKETED)
            self.assertEqual(final_trip.status, TripStatus.TICKETED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
