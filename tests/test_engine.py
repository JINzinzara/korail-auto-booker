"""engine의 domain, 후보 선택, SQLite claim 흐름 확인"""

import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

from korail_booker.domain import Candidate, SeatOption, Trip, TripStatus
from korail_booker.engine import pick_candidate
from korail_booker.storage import TripStore


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


class Phase1Test(unittest.TestCase):
    def test_trip_validation_no_passenger_limit(self) -> None:
        """승객 수의 상한 없이 필수 여행 조건만 검증하여 확인"""
        self.assertEqual(make_trip(passenger_count=3).passenger_count, 3)
        with self.assertRaises(ValueError):
            make_trip(arrival_station="서울")

    def test_candidate_selection(self) -> None:
        """열차 종류를 필터링하고 전 구간 좌석을 우선 여부 확인"""
        trip = make_trip(allow_merge_seat=True)
        selected = pick_candidate(
            trip,
            (
                make_candidate("001", 8, train_type="ITX"),
                make_candidate("003", 9, seat_option=SeatOption.MERGE),
                make_candidate("005", 10),
            ),
        )
        self.assertEqual(selected, make_candidate("005", 10))
        self.assertIsNone(
            pick_candidate(
                make_trip(), (make_candidate("003", 9, seat_option=SeatOption.MERGE),)
            )
        )

    def test_sqlite_allows(self) -> None:
        """SQLite가 한 여행에 하나의 활성 claim만 허용하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = store.create_trip(make_trip())
            self.assertTrue(store.start_trip(trip.id))
            self.assertIsNotNone(
                store.claim_candidate(trip.id, make_candidate("001", 9))
            )
            self.assertIsNone(store.claim_candidate(trip.id, make_candidate("003", 10)))
            self.assertEqual(store.get_trip(trip.id).status, TripStatus.CLAIMING)


if __name__ == "__main__":
    unittest.main()
