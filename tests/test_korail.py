"""KORAIL 조회 질의와 내부 후보 변환을 offline으로 확인"""

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import Mock

import korail_mobile_api as korail

from korail_booker.domain import (
    AttemptStatus,
    PaymentOutcomeUnknownError,
    Reservation,
    SeatOption,
    Trip,
    TripStatus,
)
from korail_booker.korail import (
    candidates_result,
    create_client,
    create_live_worker,
    login_client,
    pay_reservation_live,
    preview_reservation,
    reserve_and_cancel_live,
    reserve_live,
    search_candidates,
    search_query,
    ticket_is_issued,
    train_candidate,
)
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


def make_hold() -> korail.ReservationHoldResponse:
    """결제 테스트에 사용할 식별값 비공개 미결제 예약 생성"""
    return korail.ReservationHoldResponse(
        str_result="SUCC",
        pnr_no="hidden",
        window_no="001",
        temporary_job_sequence_1="1",
        received_amount="118000",
        journeys=(korail.ReservationJourney(reservation_change_no="0"),),
    )


def make_reservation() -> Reservation:
    """결제와 발권 조회 테스트에 사용할 내부 예약 생성"""
    return Reservation(
        reference="hidden",
        amount=118_000,
        window_no="001",
        job_sequence_1="1",
        change_no="0",
    )


def make_card() -> korail.CardPayment:
    """네트워크로 전송되지 않는 결제 테스트용 카드 입력 생성"""
    return korail.CardPayment(
        card_number="0",
        card_password="00",
        card_expire="0000",
        birthday="000000",
    )


class KorailGatewayTest(unittest.TestCase):
    """외부 조회 계약이 내부 계약으로 안전하게 변환되는지 확인"""

    def test_create_client_enables_dynapath(self) -> None:
        """라이브 client가 DynaPath를 명시적으로 활성화하는지 확인"""
        client = create_client()
        try:
            show_flow(
                "라이브 client 보안 설정",
                "입력: create_client()",
                f"출력 DynaPath 활성화: {client.config.dynapath.enabled}",
            )
            self.assertTrue(client.config.dynapath.enabled)
        finally:
            client.close()

    def test_login_client_passes_credentials_without_storing_them(self) -> None:
        """로그인 자격증명을 client에만 전달하는지 확인"""
        client = Mock(spec=korail.KorailClient)
        session = Mock(spec=korail.KorailSession)
        client.login.return_value = session

        result = login_client(client, "member", "password")

        show_flow(
            "로그인 연결",
            "입력: 호출자가 제공한 회원 식별자와 비밀번호",
            "호출: KorailClient.login 1회",
            "출력: 인증 세션",
        )
        client.login.assert_called_once_with("member", "password")
        self.assertIs(result, session)

    def test_search_query(self) -> None:
        """여행의 역, 날짜, 시작시각, 승객 수를 조회에 반영하는지 확인"""
        trip = make_trip()
        query = search_query(trip)
        show_flow(
            "여행 → KORAIL 조회",
            "입력: 서울 → 부산, 2026-10-01 08:30~12:00, KTX, 승객 2명",
            f"출력: {query.departure_station_code} → {query.arrival_station_code}, "
            f"date={query.departure_date}, time={query.departure_time}, "
            f"passengers={query.passengers}",
        )
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

        candidates = candidates_result(result)
        show_flow(
            "KORAIL 응답 → 내부 후보",
            "입력: 001 code=11 / 003 code=13 merge=A / 005 code=13 merge=N",
            "판정: 001=FULL / 003=MERGE / 005=제외",
            "출력: "
            + ", ".join(
                f"{candidate.train_no} {candidate.train_type} {candidate.seat_option}"
                for candidate in candidates
            ),
        )

        self.assertEqual(
            [candidate.train_no for candidate in candidates], ["001", "003"]
        )
        self.assertEqual(candidates[0].seat_option, SeatOption.FULL)
        self.assertEqual(candidates[1].seat_option, SeatOption.MERGE)

    def test_candidate_handles_next_day_arrival(self) -> None:
        """자정 이후 도착 열차의 도착일을 다음 날로 계산하는지 확인"""
        candidate = train_candidate(
            make_train(departure_time="235000", arrival_time="013000")
        )
        show_flow(
            "자정 통과 시각 변환",
            "입력: 20261001, 출발=235000, 도착=013000",
            f"출력 출발: {candidate.departure_at.isoformat(sep=' ')}",
            f"출력 도착: {candidate.arrival_at.isoformat(sep=' ')}",
        )
        self.assertEqual(candidate.departure_at, datetime(2026, 10, 1, 23, 50))
        self.assertEqual(candidate.arrival_at, datetime(2026, 10, 2, 1, 30))

    def test_search_candidates_calls_read_api(self) -> None:
        """실제 client 연결 함수가 읽기 API 결과만 내부 후보로 변환하는지 확인"""
        trip = make_trip()
        client = Mock(spec=korail.KorailClient)
        client.search_trains.return_value = korail.TrainSearchResult(
            trains=[make_train()],
            response=korail.BaseKorailResponse(),
        )

        candidates = search_candidates(client, trip)

        show_flow(
            "온라인 조회 연결",
            "입력: 서울 → 부산 여행 조건",
            "호출: KorailClient.search_trains 1회",
            f"출력: {candidates[0].train_no} {candidates[0].seat_option}",
        )
        client.search_trains.assert_called_once_with(search_query(trip))
        self.assertEqual(candidates[0].train_no, "001")

    def test_reservation_preview_revalidates_without_sending(self) -> None:
        """최신 열차를 다시 확인하고 예약 요청을 dry-run으로만 만드는지 확인"""
        trip = make_trip()
        train = make_train()
        candidate = train_candidate(train)
        passengers = korail.KorailPassengerCounts(adult=2)
        preview = korail.MutationPreview(
            category="reserve",
            method="POST",
            route="/reservation",
            payload={"train": "001"},
        )
        client = Mock(spec=korail.KorailClient)
        client.search_trains.return_value = korail.TrainSearchResult(
            trains=[train],
            response=korail.BaseKorailResponse(),
        )
        client.reserve.return_value = preview

        result = preview_reservation(client, trip, candidate, passengers)

        show_flow(
            "예약 dry-run",
            "입력: KTX 001 FULL, 성인 2명",
            "호출: 최신 좌석 재조회 → 예약 payload 생성",
            f"출력: {result.category}, {result.note}",
        )
        client.reserve.assert_called_once_with(
            train,
            consent=korail.MutationConsent(allow_reserve=True),
            passengers=passengers,
        )
        self.assertEqual(result.note, "dry-run: not sent")

    def test_live_reservation_requires_explicit_approval(self) -> None:
        """승인 없는 실예약 요청이 조회 전부터 차단되는지 확인"""
        client = Mock(spec=korail.KorailClient)

        with self.assertRaisesRegex(PermissionError, "explicit approval"):
            reserve_and_cancel_live(
                client,
                make_trip(),
                train_candidate(make_train()),
                korail.KorailPassengerCounts(adult=2),
                120_000,
            )

        show_flow(
            "실예약 승인 차단",
            "입력: approved=False",
            "출력: 외부 API 호출 0회, PermissionError",
        )
        client.search_trains.assert_not_called()
        client.reserve.assert_not_called()

    def test_live_reservation_confirms_fare_and_cancels(self) -> None:
        """실예약 금액을 상세 조회와 대조한 뒤 미결제 예약을 취소하는지 확인"""
        trip = make_trip()
        train = make_train()
        candidate = train_candidate(train)
        passengers = korail.KorailPassengerCounts(adult=2)
        hold = korail.ReservationHoldResponse(
            pnr_no="hidden", received_amount="118000"
        )
        client = Mock(spec=korail.KorailClient)
        client.search_trains.return_value = korail.TrainSearchResult(
            trains=[train], response=korail.BaseKorailResponse()
        )
        client.reserve.return_value = hold
        client.get_ticket_reservation_detail.return_value = (
            korail.TicketReservationDetailResponse(total_received_amount="000118000")
        )
        client.cancel_unpaid_hold.return_value = korail.BaseKorailResponse(
            str_result="SUCC"
        )

        fare = reserve_and_cancel_live(
            client,
            trip,
            candidate,
            passengers,
            120_000,
            approved=True,
        )

        show_flow(
            "실예약 안전 점검",
            "입력: KTX 001, 승인 상한 120,000원",
            f"출력: 확정 운임 {fare:,}원",
            "복구: 결제 없이 예약 취소 확인",
        )
        client.reserve.assert_called_once_with(
            train,
            consent=korail.MutationConsent(allow_reserve=True, dry_run=False),
            passengers=passengers,
        )
        client.cancel_unpaid_hold.assert_called_once_with(
            hold,
            consent=korail.MutationConsent(allow_cancel=True, dry_run=False),
        )
        self.assertEqual(fare, 118_000)

    def test_live_reservation_cancels_when_fare_exceeds_limit(self) -> None:
        """확정 운임이 승인 상한을 넘더라도 미결제 예약을 취소하는지 확인"""
        trip = make_trip()
        train = make_train()
        hold = korail.ReservationHoldResponse(
            pnr_no="hidden", received_amount="118000"
        )
        client = Mock(spec=korail.KorailClient)
        client.search_trains.return_value = korail.TrainSearchResult(
            trains=[train], response=korail.BaseKorailResponse()
        )
        client.reserve.return_value = hold
        client.get_ticket_reservation_detail.return_value = (
            korail.TicketReservationDetailResponse(total_received_amount="118000")
        )
        client.cancel_unpaid_hold.return_value = korail.BaseKorailResponse(
            str_result="SUCC"
        )

        with self.assertRaisesRegex(ValueError, "exceeds"):
            reserve_and_cancel_live(
                client,
                trip,
                train_candidate(train),
                korail.KorailPassengerCounts(adult=2),
                100_000,
                approved=True,
            )

        show_flow(
            "운임 상한 초과 복구",
            "입력: 확정 118,000원, 승인 상한 100,000원",
            "출력: 결제 차단, ValueError",
            "복구: 미결제 예약 취소 확인",
        )
        client.cancel_unpaid_hold.assert_called_once()

    def test_live_reservation_returns_restart_safe_record(self) -> None:
        """실예약 응답을 외부 모델 없는 재시작 복구값으로 변환하는지 확인"""
        trip = make_trip()
        train = make_train()
        client = Mock(spec=korail.KorailClient)
        client.search_trains.return_value = korail.TrainSearchResult(
            trains=[train], response=korail.BaseKorailResponse()
        )
        client.reserve.return_value = make_hold()
        client.get_ticket_reservation_detail.return_value = (
            korail.TicketReservationDetailResponse(total_received_amount="118000")
        )

        reservation = reserve_live(
            client,
            trip,
            train_candidate(train),
            korail.KorailPassengerCounts(adult=2),
            120_000,
            approved=True,
        )

        show_flow(
            "실예약 복구값 변환",
            "입력: KTX 예약 응답과 확정 운임 118,000원",
            "출력: worker·SQLite용 내부 예약 생성",
            "예약번호 출력: 없음",
        )
        self.assertEqual(reservation, make_reservation())
        client.cancel_unpaid_hold.assert_not_called()

    def test_live_payment_requires_explicit_charge_approval(self) -> None:
        """실카드 결제 승인이 없으면 조회와 결제를 모두 차단하는지 확인"""
        client = Mock(spec=korail.KorailClient)

        with self.assertRaisesRegex(PermissionError, "explicit approval"):
            pay_reservation_live(client, make_reservation(), make_card(), 120_000)

        show_flow(
            "실결제 승인 차단",
            "입력: real_charge_approved=False",
            "출력: 외부 API 호출 0회, PermissionError",
        )
        client.get_ticket_reservation_detail.assert_not_called()
        client.pay_with_card.assert_not_called()

    def test_live_payment_rechecks_fare_and_verifies_ticket(self) -> None:
        """결제 직전 운임을 재검증하고 발권을 별도 조회하는지 확인"""
        reservation = make_reservation()
        card = make_card()
        payment = korail.ReservationPaymentResponse(str_result="SUCC")
        client = Mock(spec=korail.KorailClient)
        client.get_ticket_reservation_detail.return_value = (
            korail.TicketReservationDetailResponse(total_received_amount="000118000")
        )
        client.pay_with_card.return_value = payment
        client.get_ticket_list.return_value = korail.BaseKorailResponse(
            raw={"tickets": [{"h_pnr_no": "hidden"}]}
        )

        result = pay_reservation_live(
            client,
            reservation,
            card,
            120_000,
            real_charge_approved=True,
        )
        issued = ticket_is_issued(client, reservation)

        show_flow(
            "실결제 및 발권 확인",
            "입력: 확정 118,000원, 승인 상한 120,000원, 일회성 실결제 승인",
            "호출: 상세 운임 재조회 → 실결제 1회 → 승차권 목록 조회",
            f"출력: 결제={result}, 발권={issued}",
            "카드정보·예약번호 출력: 없음",
        )
        client.pay_with_card.assert_called_once_with(
            make_hold(),
            card,
            consent=korail.MutationConsent(
                allow_payment=True,
                dry_run=False,
                fake_card_only=False,
                real_card_acknowledged=True,
            ),
        )
        self.assertTrue(result)
        self.assertTrue(issued)

    def test_declined_payment_cancels_unpaid_reservation(self) -> None:
        """결제 거절 응답이면 미결제 상태를 확인해 예약을 취소하는지 확인"""
        reservation = make_reservation()
        client = Mock(spec=korail.KorailClient)
        client.get_ticket_reservation_detail.return_value = (
            korail.TicketReservationDetailResponse(total_received_amount="118000")
        )
        client.pay_with_card.return_value = korail.ReservationPaymentResponse(
            str_result="FAIL"
        )
        client.cancel_unpaid_hold.return_value = korail.BaseKorailResponse(
            str_result="SUCC"
        )

        result = pay_reservation_live(
            client,
            reservation,
            make_card(),
            120_000,
            real_charge_approved=True,
        )

        show_flow(
            "결제 거절 복구",
            "입력: 서버 결제 결과=FAIL",
            f"출력: 결제={result}",
            "복구: 미결제 예약 취소 확인",
        )
        self.assertFalse(result)
        client.cancel_unpaid_hold.assert_called_once()

    def test_unknown_payment_outcome_is_not_retried_or_cancelled(self) -> None:
        """결제 응답이 불명확하면 재결제나 취소 없이 조회 복구로 넘기는지 확인"""
        reservation = make_reservation()
        client = Mock(spec=korail.KorailClient)
        client.get_ticket_reservation_detail.return_value = (
            korail.TicketReservationDetailResponse(total_received_amount="118000")
        )
        client.pay_with_card.side_effect = TimeoutError("response unavailable")

        with self.assertRaisesRegex(PaymentOutcomeUnknownError, "outcome is unknown"):
            pay_reservation_live(
                client,
                reservation,
                make_card(),
                120_000,
                real_charge_approved=True,
            )

        show_flow(
            "결제 결과 불명확",
            "입력: 결제 전송 뒤 TimeoutError",
            "출력: 결과 불명확, 조회 복구 필요",
            "안전조치: 재결제 0회, 자동취소 0회",
        )
        self.assertEqual(client.pay_with_card.call_count, 1)
        client.cancel_unpaid_hold.assert_not_called()

    def test_live_gateway_connects_to_worker_end_to_end(self) -> None:
        """조회부터 발권 확인까지 실제 gateway 계약이 worker에 연결되는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "booker.sqlite3")
            trip = store.create_trip(make_trip())
            store.start_trip(trip.id)
            train = make_train()
            client = Mock(spec=korail.KorailClient)
            client.search_trains.return_value = korail.TrainSearchResult(
                trains=[train], response=korail.BaseKorailResponse()
            )
            client.reserve.return_value = make_hold()
            client.get_ticket_reservation_detail.return_value = (
                korail.TicketReservationDetailResponse(
                    total_received_amount="118000"
                )
            )
            client.pay_with_card.return_value = korail.ReservationPaymentResponse(
                str_result="SUCC"
            )
            client.get_ticket_list.return_value = korail.BaseKorailResponse(
                raw={"tickets": [{"h_pnr_no": "hidden"}]}
            )
            worker = create_live_worker(
                store,
                client,
                korail.KorailPassengerCounts(adult=2),
                make_card(),
                120_000,
                reserve_approved=True,
                real_charge_approved=True,
            )

            result = worker.run_once(trip.id)

            show_flow(
                "라이브 gateway → worker 통합",
                "입력: 서울→부산, 성인 2명, 상한 120,000원, 예약·결제 승인",
                "흐름: 조회 → 재조회·예약 → SQLite 저장 → 결제 → 승차권 확인",
                f"출력 구매/여행 상태: {result.status} / {store.get_trip(trip.id).status}",
                "카드정보·예약번호 출력: 없음",
            )
            self.assertEqual(result.status, AttemptStatus.TICKETED)
            self.assertEqual(store.get_trip(trip.id).status, TripStatus.TICKETED)
            self.assertEqual(client.search_trains.call_count, 2)
            client.reserve.assert_called_once()
            client.pay_with_card.assert_called_once()
            client.get_ticket_list.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
