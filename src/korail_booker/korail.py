"""DynaPath 사용 KORAIL 조회와 내부 여행 후보 변환"""

from datetime import datetime, timedelta

import korail_mobile_api as korail

from .domain import Candidate, SeatOption, Trip


def create_client() -> korail.KorailClient:
    """라이브러리 지원 DynaPath를 활성화한 KORAIL client 생성"""
    return korail.KorailClient(korail.KorailConfig(enable_dynapath=True))


def search_query(trip: Trip) -> korail.TrainSearchQuery:
    """내부 여행 조건을 KORAIL 열차 조회 질의로 변환"""
    return korail.TrainSearchQuery(
        departure_station_code=trip.departure_station,
        arrival_station_code=trip.arrival_station,
        departure_date=trip.travel_date.strftime("%Y%m%d"),
        departure_time=trip.earliest_departure.strftime("%H%M%S"),
        passengers=trip.passenger_count,
    )


def train_candidate(train: korail.TrainSummary) -> Candidate | None:
    """예약 가능한 KORAIL 열차 행을 내부 후보 하나로 변환"""
    if train.general_reservation_code == "11":
        seat_option = SeatOption.FULL
    elif train.merge_seat_application_flag in (
        korail.KORAIL_MERGE_SEAT_FLAGS_BY_CABIN["1"]
    ):
        seat_option = SeatOption.MERGE
    else:
        return None

    train_type = train.train_class_name or train.train_group_name
    if not train.train_no or not train_type:
        raise ValueError("KORAIL train number and type are required")
    departure_at = _schedule_datetime(train.departure_date, train.departure_time)
    arrival_at = _schedule_datetime(train.departure_date, train.arrival_time)
    if arrival_at <= departure_at:
        arrival_at += timedelta(days=1)
    return Candidate(
        train_no=train.train_no,
        train_type=train_type,
        departure_at=departure_at,
        arrival_at=arrival_at,
        seat_option=seat_option,
    )


def candidates_result(result: korail.TrainSearchResult) -> tuple[Candidate, ...]:
    """KORAIL 검색 결과에서 예약 가능한 내부 후보만 반환"""
    candidates = (train_candidate(train) for train in result.trains)
    return tuple(candidate for candidate in candidates if candidate is not None)


def search_candidates(
    client: korail.KorailClient, trip: Trip
) -> tuple[Candidate, ...]:
    """KORAIL 읽기 API를 한 번 호출해 예약 가능한 내부 후보를 반환"""
    return candidates_result(client.search_trains(search_query(trip)))


def _schedule_datetime(date_value: str | None, time_value: str | None) -> datetime:
    """KORAIL 날짜와 시각 문자열을 로컬 datetime으로 변환"""
    if not date_value or not time_value:
        raise ValueError("KORAIL train date and time are required")
    try:
        return datetime.strptime(date_value + time_value.zfill(6), "%Y%m%d%H%M%S")
    except ValueError as error:
        raise ValueError("invalid KORAIL train date or time") from error
