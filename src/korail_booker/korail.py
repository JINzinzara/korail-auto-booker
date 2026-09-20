"""DynaPath 사용 KORAIL 조회와 내부 여행 후보 변환"""

import os
from datetime import datetime, timedelta

import korail_mobile_api as korail

from .domain import (
    Candidate,
    PaymentOutcomeUnknownError,
    PurchaseAttempt,
    Reservation,
    SeatOption,
    Trip,
)
from .storage import TripStore
from .worker import BookingWorker


def create_client() -> korail.KorailClient:
    """라이브러리 지원 DynaPath를 활성화한 KORAIL client 생성"""
    return korail.KorailClient(korail.KorailConfig(enable_dynapath=True))


def create_live_client() -> korail.KorailClient:
    """승인 환경과 고정 기기 정보로 실제 KORAIL client 생성"""
    if os.environ.get("KORAIL_MOBILE_API_LIVE") != "1":
        raise PermissionError("KORAIL_MOBILE_API_LIVE=1 is required")
    return korail.KorailClient(korail.build_config_from_env())


def close_client(client: korail.KorailClient) -> None:
    """서버 로그인 세션과 로컬 HTTP 연결을 순서대로 종료"""
    try:
        client.logout()
    finally:
        client.close()


def create_adult_passengers(count: int) -> korail.KorailPassengerCounts:
    """성인 인원수를 KORAIL 예약용 승객 구성으로 변환"""
    return korail.KorailPassengerCounts(adult=count)


def create_card_payment(
    card_number: str,
    card_password: str,
    card_expire: str,
    birthday: str,
) -> korail.CardPayment:
    """검증한 개인카드 입력을 로그나 저장소를 거치지 않고 생성"""
    fields = {
        "card number": (card_number, None),
        "card password": (card_password, 2),
        "card expiry": (card_expire, 4),
        "birthday": (birthday, 6),
    }
    for name, (value, length) in fields.items():
        if not value.isdecimal() or (length is not None and len(value) != length):
            raise ValueError(f"{name} has an invalid format")
    return korail.CardPayment(
        card_number=card_number,
        card_password=card_password,
        card_expire=card_expire,
        birthday=birthday,
    )


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
    hold, confirmed_amount = _reserve_live(
        client,
        trip,
        candidate,
        passengers,
        max_fare_won,
        approved=approved,
    )
    try:
        return confirmed_amount
    finally:
        _cancel_unpaid(client, hold)


def reserve_live(
    client: korail.KorailClient,
    trip: Trip,
    candidate: Candidate,
    passengers: korail.KorailPassengerCounts,
    max_fare_won: int,
    *,
    approved: bool = False,
) -> Reservation:
    """명시적 승인과 운임 상한을 확인하고 결제 전 실예약을 반환"""
    hold, confirmed_amount = _reserve_live(
        client,
        trip,
        candidate,
        passengers,
        max_fare_won,
        approved=approved,
    )
    try:
        return _reservation_from_hold(hold, confirmed_amount)
    except Exception:
        _cancel_unpaid(client, hold)
        raise
    try:
        return _reservation_from_hold(hold, confirmed_amount)
    except Exception:
        _cancel_unpaid(client, hold)
        raise


def pay_reservation_live(
    client: korail.KorailClient,
    reservation: Reservation,
    card: korail.CardPayment,
    max_fare_won: int,
    *,
    real_charge_approved: bool = False,
) -> bool:
    """실카드 일회성 승인과 운임 재검증 뒤 예약을 한 번 결제"""
    if real_charge_approved is not True:
        raise PermissionError("real-card payment requires explicit approval")
    _validate_max_fare(max_fare_won)
    hold = _hold_from_reservation(reservation)
    hold = _hold_from_reservation(reservation)
    try:
        _confirmed_fare(client, hold, max_fare_won)
    except Exception:
        _cancel_unpaid(client, hold)
        raise
    try:
        response = client.pay_with_card(
            hold,
            card,
            consent=korail.MutationConsent(
                allow_payment=True,
                dry_run=False,
                fake_card_only=False,
                real_card_acknowledged=True,
            ),
        )
    except Exception as error:
        raise PaymentOutcomeUnknownError(
            "payment outcome is unknown; reconcile before retrying"
        ) from error
    if not isinstance(response, korail.ReservationPaymentResponse):
        raise PaymentOutcomeUnknownError(
            "payment outcome is unknown; reconcile before retrying"
        )
        raise PaymentOutcomeUnknownError(
            "payment outcome is unknown; reconcile before retrying"
        )
    if response.str_result != "SUCC":
        _cancel_unpaid(client, hold)
        return False
    return True


def ticket_is_issued(client: korail.KorailClient, reservation: Reservation) -> bool:
    """현재 승차권 목록에서 지정 예약번호의 발권 여부를 확인"""
    return _pnr_present(client.get_ticket_list().raw, reservation.reference)


def create_live_worker(
    store: TripStore,
    client: korail.KorailClient,
    passengers: korail.KorailPassengerCounts,
    card: korail.CardPayment,
    max_fare_won: int,
    *,
    reserve_approved: bool = False,
    real_charge_approved: bool = False,
) -> BookingWorker:
    """인증된 KORAIL client와 안전 경계를 실제 worker 흐름에 연결"""

    def search(trip: Trip) -> tuple[Candidate, ...]:
        """worker 여행 조건으로 KORAIL 후보를 조회"""
        return search_candidates(client, trip)

    def reserve(trip: Trip, candidate: Candidate) -> Reservation:
        """worker 후보를 승인된 실예약으로 확보"""
        return reserve_live(
            client,
            trip,
            candidate,
            passengers,
            max_fare_won,
            approved=reserve_approved,
        )

    def pay(attempt: PurchaseAttempt) -> bool:
        """저장된 예약을 승인된 실카드로 한 번 결제"""
        if attempt.reservation is None:
            raise RuntimeError("payment attempt has no reservation")
        return pay_reservation_live(
            client,
            attempt.reservation,
            card,
            max_fare_won,
            real_charge_approved=real_charge_approved,
        )

    def verify(attempt: PurchaseAttempt) -> bool:
        """저장된 예약이 현재 승차권 목록에 발권됐는지 조회"""
        if attempt.reservation is None:
            raise RuntimeError("ticket check has no reservation")
        return ticket_is_issued(client, attempt.reservation)

    return BookingWorker(store, search, reserve, pay, verify)


def _reserve_live(
    client: korail.KorailClient,
    trip: Trip,
    candidate: Candidate,
    passengers: korail.KorailPassengerCounts,
    max_fare_won: int,
    *,
    approved: bool,
) -> tuple[korail.ReservationHoldResponse, int]:
    """실예약을 만들고 금액 검증 실패 시 즉시 취소"""
    if approved is not True:
        raise PermissionError("live reservation requires explicit approval")
    _validate_max_fare(max_fare_won)
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
        return hold, _confirmed_fare(client, hold, max_fare_won)
    except Exception:
        _cancel_unpaid(client, hold)
        raise


def _confirmed_fare(
    client: korail.KorailClient,
    hold: korail.ReservationHoldResponse,
    max_fare_won: int,
) -> int:
    """예약 응답과 상세 조회 금액이 같고 승인 상한 이하인지 확인"""
    if not hold.pnr_no:
        raise RuntimeError("reservation hold has no reservation number")
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


def _reservation_from_hold(
    hold: korail.ReservationHoldResponse, confirmed_amount: int
) -> Reservation:
    """KORAIL 예약 응답을 재시작 가능한 내부 예약으로 변환"""
    if not hold.pnr_no or not hold.window_no:
        raise RuntimeError("reservation hold lacks payment identifiers")
    change_no = hold.journeys[0].reservation_change_no if hold.journeys else None
    return Reservation(
        reference=hold.pnr_no,
        amount=confirmed_amount,
        window_no=hold.window_no,
        job_sequence_1=hold.temporary_job_sequence_1 or None,
        job_sequence_2=hold.temporary_job_sequence_2 or None,
        change_no=change_no or None,
    )


def _hold_from_reservation(reservation: Reservation) -> korail.ReservationHoldResponse:
    """저장된 내부 예약을 KORAIL 결제 입력으로 복원"""
    journeys = (
        (korail.ReservationJourney(reservation_change_no=reservation.change_no),)
        if reservation.change_no is not None
        else ()
    )
    return korail.ReservationHoldResponse(
        str_result="SUCC",
        pnr_no=reservation.reference,
        window_no=reservation.window_no,
        temporary_job_sequence_1=reservation.job_sequence_1,
        temporary_job_sequence_2=reservation.job_sequence_2,
        received_amount=str(reservation.amount),
        journeys=journeys,
    )


def _reservation_from_hold(
    hold: korail.ReservationHoldResponse, confirmed_amount: int
) -> Reservation:
    """KORAIL 예약 응답을 재시작 가능한 내부 예약으로 변환"""
    if not hold.pnr_no or not hold.window_no:
        raise RuntimeError("reservation hold lacks payment identifiers")
    change_no = hold.journeys[0].reservation_change_no if hold.journeys else None
    return Reservation(
        reference=hold.pnr_no,
        amount=confirmed_amount,
        window_no=hold.window_no,
        job_sequence_1=hold.temporary_job_sequence_1 or None,
        job_sequence_2=hold.temporary_job_sequence_2 or None,
        change_no=change_no or None,
    )


def _hold_from_reservation(reservation: Reservation) -> korail.ReservationHoldResponse:
    """저장된 내부 예약을 KORAIL 결제 입력으로 복원"""
    journeys = (
        (korail.ReservationJourney(reservation_change_no=reservation.change_no),)
        if reservation.change_no is not None
        else ()
    )
    return korail.ReservationHoldResponse(
        str_result="SUCC",
        pnr_no=reservation.reference,
        window_no=reservation.window_no,
        temporary_job_sequence_1=reservation.job_sequence_1,
        temporary_job_sequence_2=reservation.job_sequence_2,
        received_amount=str(reservation.amount),
        journeys=journeys,
    )


def _cancel_unpaid(
    client: korail.KorailClient, hold: korail.ReservationHoldResponse
) -> None:
    """미결제 예약을 취소하고 서버 성공 응답까지 확인"""
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


def _validate_max_fare(max_fare_won: int) -> None:
    """승인 운임 상한이 양의 정수인지 확인"""
    if (
        isinstance(max_fare_won, bool)
        or not isinstance(max_fare_won, int)
        or max_fare_won < 1
    ):
        raise ValueError("maximum fare must be a positive integer")


def _pnr_present(raw: object, pnr_no: str) -> bool:
    """중첩된 승차권 응답에서 지정 예약번호를 재귀적으로 탐색"""
    if isinstance(raw, dict):
        if raw.get("h_pnr_no") == pnr_no or raw.get("pnrNo") == pnr_no:
            return True
        return any(_pnr_present(value, pnr_no) for value in raw.values())
    if isinstance(raw, list):
        return any(_pnr_present(value, pnr_no) for value in raw)
    return False


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
