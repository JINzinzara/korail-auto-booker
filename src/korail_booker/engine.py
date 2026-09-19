"""여행 조건에 맞는 최우선 후보 선택"""

from collections.abc import Iterable

from .domain import Candidate, SeatOption, Trip


def pick_candidate(trip: Trip, candidates: Iterable[Candidate]) -> Candidate | None:
    """여행 조건을 충족하는 후보 중 전 구간 좌석 우선으로 하나를 선택"""
    eligible = (
        candidate
        for candidate in candidates
        if candidate.train_type in trip.train_types
        and candidate.departure_at.date() == trip.travel_date
        and trip.earliest_departure
        <= candidate.departure_at.time()
        <= trip.latest_departure
        and (candidate.seat_option is SeatOption.FULL or trip.allow_merge_seat)
    )
    return min(
        eligible,
        key=lambda candidate: (
            candidate.seat_option is SeatOption.MERGE,
            candidate.departure_at,
            candidate.arrival_at,
            candidate.train_no,
        ),
        default=None,
    )
