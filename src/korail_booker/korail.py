"""DynaPath 사용 KORAIL 조회와 내부 여행 후보 변환"""

from datetime import datetime, timedelta

import korail_mobile_api as korail

from .domain import Candidate, SeatOption, Trip


def create_client() -> korail.KorailClient:
    """라이브러리 지원 DynaPath를 활성화한 KORAIL client 생성"""
    return korail.KorailClient(korail.KorailConfig(enable_dynapath=True))


def login_client(
    client: korail.KorailClient, member_no: str, password: str
) -> korail.KorailSession:
    """호출자가 제공한 자격증명으로 KORAIL 세션 시작"""
    return client.login(member_no, password)


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


def search_candidates(client: korail.KorailClient, trip: Trip) -> tuple[Candidate, ...]:
    """KORAIL 읽기 API를 한 번 호출해 예약 가능한 내부 후보를 반환"""
    return candidates_result(client.search_trains(search_query(trip)))


def preview_reservation(
    client: korail.KorailClient,
    trip: Trip,
    candidate: Candidate,
    passengers: korail.KorailPassengerCounts,
) -> korail.MutationPreview:
    """후보를 다시 확인하고 전 구간 좌석 예약 요청을 전송 없이 생성"""
    _validate_reservation(trip, candidate, passengers)
    train = _find_train(client, trip, candidate)
    if train is None:
        raise ValueError("candidate is no longer available")
    preview = client.reserve(
        train,
        consent=korail.MutationConsent(allow_reserve=True),
        passengers=passengers,
    )
    if not isinstance(preview, korail.MutationPreview):
        raise RuntimeError("dry-run unexpectedly changed KORAIL state")
    return preview


def reserve_and_cancel_live(
    client: korail.KorailClient,
    trip: Trip,
    candidate: Candidate,
    passengers: korail.KorailPassengerCounts,
    max_fare_won: int,
    *,
    approved: bool = False,
) -> int:
    """명시적 승인으로 예약한 뒤 운임을 검증하고 미결제 예약은 취소"""
    if approved is not True:
        raise PermissionError("live reservation requires explicit approval")
    if (
        isinstance(max_fare_won, bool)
        or not isinstance(max_fare_won, int)
        or max_fare_won < 1
    ):
        raise ValueError("maximum fare must be a positive integer")
    _validate_reservation(trip, candidate, passengers)
    train = _find_train(client, trip, candidate)
    if train is None:
        raise ValueError("candidate is no longer available")
    hold = client.reserve(
        train,
        consent=korail.MutationConsent(allow_reserve=True, dry_run=False),
        passengers=passengers,
    )
    if not isinstance(hold, korail.ReservationHoldResponse) or not hold.pnr_no:
        raise RuntimeError("live reservation did not return a reservation hold")

    try:
        detail = client.get_ticket_reservation_detail(
            korail.TicketReservationDetailRequest(pnr_no=hold.pnr_no)
        )
        held_amount = _won(hold.received_amount)
        confirmed_amount = _won(detail.total_received_amount)
        if held_amount != confirmed_amount:
            raise RuntimeError("reservation amounts do not match")
        if confirmed_amount > max_fare_won:
            raise ValueError("confirmed fare exceeds the approved maximum")
        return confirmed_amount
    finally:
        try:
            cancelled = client.cancel_unpaid_hold(
                hold,
                consent=korail.MutationConsent(allow_cancel=True, dry_run=False),
            )
        except Exception as error:
            raise RuntimeError("failed to cancel the unpaid reservation") from error
        if (
            not isinstance(cancelled, korail.BaseKorailResponse)
            or cancelled.str_result != "SUCC"
        ):
            raise RuntimeError("failed to confirm unpaid reservation cancellation")


def _validate_reservation(
    trip: Trip,
    candidate: Candidate,
    passengers: korail.KorailPassengerCounts,
) -> None:
    """여행 인원수와 구현된 좌석 유형만 예약 입력으로 허용"""
    if passengers.total != trip.passenger_count:
        raise ValueError("passenger counts must match the trip")
    if candidate.seat_option is not SeatOption.FULL:
        raise ValueError("merge-seat reservation is not implemented")


def _find_train(
    client: korail.KorailClient, trip: Trip, candidate: Candidate
) -> korail.TrainSummary | None:
    """최신 조회 결과에서 내부 후보와 정확히 일치하는 KORAIL 열차 반환"""
    result = client.search_trains(search_query(trip))
    for train in result.trains:
        if train_candidate(train) == candidate:
            return train
    return None


def _won(value: str | None) -> int:
    """KORAIL 금액 문자열을 검증해 정수로 변환"""
    if not value or not value.isdecimal() or int(value) < 1:
        raise RuntimeError("KORAIL returned an invalid fare amount")
    return int(value)


def _schedule_datetime(date_value: str | None, time_value: str | None) -> datetime:
    """KORAIL 날짜와 시각 문자열을 로컬 datetime으로 변환"""
    if not date_value or not time_value:
        raise ValueError("KORAIL train date and time are required")
    try:
        return datetime.strptime(date_value + time_value.zfill(6), "%Y%m%d%H%M%S")
    except ValueError as error:
        raise ValueError("invalid KORAIL train date or time") from error
