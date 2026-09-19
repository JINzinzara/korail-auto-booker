"""KORAIL 조회 질의와 내부 후보 변환을 offline으로 확인"""

import unittest
from dataclasses import replace
from datetime import date, datetime, time

import korail_mobile_api as korail

from korail_booker.domain import SeatOption, Trip
from korail_booker.korail import (
    build_search_query,
    candidate_from_train,
    candidates_from_result,
)


def make_trip() -> Trip:
    """조회 질의 테스트에 사용할 기본 여행 생성"""
    return Trip(
        departure_station="서울",
        arrival_station="부산",
        travel_date=date(2026, 10, 1),
        earliest_departure=time(8, 30),
        latest_departure=time(12),
        train_types=("KTX",),
        passenger_count=2,
    )


def make_train(**changes: object) -> korail.TrainSummary:
    """후보 변환 테스트에 사용할 KORAIL 열차 행 생성"""
    train = korail.TrainSummary(
        train_no="001",
        train_class_name="KTX",
        departure_date="20261001",
        departure_time="090000",
        arrival_time="120000",
        general_reservation_code="11",
    )
    return replace(train, **changes)


class KorailGatewayTest(unittest.TestCase):
    """외부 조회 계약이 내부 계약으로 안전하게 변환되는지 확인"""

    def test_search_query(self) -> None:
        """여행의 역, 날짜, 시작시각, 승객 수를 조회 질의에 반영하는지 확인"""
        query = build_search_query(make_trip())
        self.assertEqual(query.departure_station_code, "서울")
        self.assertEqual(query.arrival_station_code, "부산")
        self.assertEqual(query.departure_date, "20261001")
        self.assertEqual(query.departure_time, "083000")
        self.assertEqual(query.passengers, 2)

    def test_candidates_result(self) -> None:
        """전 구간 좌석과 좌석·입석만 내부 후보로 변환하는지 확인"""
        full = make_train()
        merge = make_train(
            train_no="003",
            departure_time="100000",
            arrival_time="130000",
            general_reservation_code="13",
            merge_seat_application_flag="A",
        )
        sold_out = make_train(
            train_no="005",
            general_reservation_code="13",
            merge_seat_application_flag="N",
        )
        result = korail.TrainSearchResult(
            trains=[full, merge, sold_out],
            response=korail.BaseKorailResponse(),
        )

        candidates = candidates_from_result(result)

        self.assertEqual(
            [candidate.train_no for candidate in candidates], ["001", "003"]
        )
        self.assertEqual(candidates[0].seat_option, SeatOption.FULL)
        self.assertEqual(candidates[1].seat_option, SeatOption.MERGE)

    def test_candidate_handles_next_day_arrival(self) -> None:
        """자정 이후 도착 열차의 도착일을 다음 날로 계산하는지 확인"""
        candidate = candidate_from_train(
            make_train(departure_time="235000", arrival_time="013000")
        )
        self.assertEqual(candidate.departure_at, datetime(2026, 10, 1, 23, 50))
        self.assertEqual(candidate.arrival_at, datetime(2026, 10, 2, 1, 30))


if __name__ == "__main__":
    unittest.main()
