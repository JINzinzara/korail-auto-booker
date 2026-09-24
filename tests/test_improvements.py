"""대화형 실행·중복 차단·기기 프로필·서버 복구를 외부 요청 없이 검증"""

import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import korail_mobile_api as korail
from korail_mobile_api.errors import KorailAppError, KorailTransportError

from korail_booker.app import main, run_trip
from korail_booker.domain import ReservationNotSentError, TripStatus
from korail_booker.korail import (
    create_live_client,
    ensure_no_conflicting_ticket,
    create_live_worker,
)
from korail_booker.storage import TripStore
from test_app import environment
from test_worker import make_trip, make_candidate, make_reservation
from test_korail import make_train, make_hold, make_card


def ticket_response(day="20261001", departure="서울", arrival="부산"):
    """고정 라이브러리 계약의 중첩 승차권 응답을 개인정보 없이 생성"""
    return korail.BaseKorailResponse(
        str_result="SUCC",
        raw={
            "reservation_list": [
                {
                    "ticket_list": [
                        {
                            "train_info": [
                                {
                                    "h_dpt_dt": day,
                                    "h_dpt_rs_stn_nm": departure,
                                    "h_arv_rs_stn_nm": arrival,
                                    "h_pnr_no": "fixture-pnr",
                                }
                            ]
                        }
                    ]
                }
            ],
        },
    )


def client_for_purchase():
    """조회·예약·운임·발권까지 실제 gateway가 사용할 모의 client 생성"""
    client = Mock(spec=korail.KorailClient)
    client.search_trains.return_value = korail.TrainSearchResult(
        trains=[make_train()], response=korail.BaseKorailResponse()
    )
    client.get_ticket_list.return_value = korail.BaseKorailResponse(
        raw={"reservation_list": []}
    )
    client.reserve.return_value = make_hold()
    client.get_ticket_reservation_detail.return_value = (
        korail.TicketReservationDetailResponse(total_received_amount="118000")
    )
    client.pay_with_card.return_value = korail.ReservationPaymentResponse(
        str_result="SUCC"
    )
    return client


class ImprovementsTest(unittest.TestCase):
    """실제 DB와 모의 API로 위험 경계와 실행 편의성을 검증"""

    def setUp(self):
        """각 검사에 독립된 SQLite와 임시 디렉터리를 제공"""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "state.sqlite3"
        self.store = TripStore(self.database)

    def test_local_rule_boundaries_reverse_and_ticketed(self):
        """날짜 ±2일·역 표기·발권 상태를 막고 반대 방향과 3일 차이를 허용"""
        trip = self.store.create_trip(make_trip())
        for offset in (-2, -1, 0, 1, 2):
            with (
                self.subTest(offset=offset),
                self.assertRaisesRegex(ValueError, "±2일"),
            ):
                self.store.create_trip(
                    replace(
                        make_trip(),
                        departure_station=" 서울역 ",
                        travel_date=trip.travel_date + timedelta(days=offset),
                    )
                )
        self.store.create_trip(
            replace(make_trip(), departure_station="부산", arrival_station="서울")
        )
        self.store.create_trip(
            replace(make_trip(), travel_date=trip.travel_date + timedelta(days=3))
        )
        self.store.start_trip(trip.id)
        attempt = self.store.claim_candidate(trip.id, make_candidate())
        self.store.start_reservation(attempt.id)
        self.store.mark_reserved(attempt.id, make_reservation())
        self.store.start_payment(attempt.id)
        self.store.mark_reconciling(attempt.id)
        self.store.mark_ticketed(attempt.id)
        with self.assertRaisesRegex(ValueError, "±2일"):
            self.store.create_trip(make_trip())

    def test_concurrent_creation_only_one_and_stopped_draft_releases(self):
        """동시 여행 생성에서도 하나만 허용하고 미실행 초안은 중지 가능"""

        def create():
            """공유 SQLite에 동일 여행을 동시에 생성"""
            try:
                return self.store.create_trip(make_trip()).id
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.store.stop_trip(next(result for result in results if result is not None))
        self.assertIsNotNone(self.store.create_trip(make_trip()).id)

    def test_account_ticket_nested_boundaries_and_unknown_schema(self):
        """계정의 중첩 승차권을 비교하고 누락·변형 응답은 차단"""
        client = client_for_purchase()
        for day in ("20260929", "20261001", "20261003"):
            client.get_ticket_list.return_value = ticket_response(day)
            with self.assertRaisesRegex(ValueError, "같은 방향"):
                ensure_no_conflicting_ticket(client, make_trip())
        for response in (
            ticket_response("20261004"),
            ticket_response(departure="부산", arrival="서울"),
        ):
            client.get_ticket_list.return_value = response
            ensure_no_conflicting_ticket(client, make_trip())
        for raw in (
            {},
            {"reservation_list": "invalid"},
            {"reservation_list": [{"ticket_list": [{"train_info": [{}]}]}]},
        ):
            client.get_ticket_list.return_value = korail.BaseKorailResponse(raw=raw)
            with self.assertRaises(ValueError):
                ensure_no_conflicting_ticket(client, make_trip())

    def test_account_conflict_prevents_reserve_and_releases_unsent_claim(self):
        """기존 표가 있으면 예약 API를 호출하지 않고 claim을 안전 해제"""
        trip = self.store.create_trip(make_trip())
        self.store.start_trip(trip.id)
        client = client_for_purchase()
        client.get_ticket_list.return_value = ticket_response()
        worker = create_live_worker(
            self.store,
            client,
            korail.KorailPassengerCounts(adult=1),
            make_card(),
            120000,
            reserve_approved=True,
            real_charge_approved=True,
        )
        with self.assertRaises(ReservationNotSentError):
            worker.run_once(trip.id)
        client.reserve.assert_not_called()
        client.pay_with_card.assert_not_called()
        self.assertEqual(self.store.get_trip(trip.id).status, TripStatus.MONITORING)
        self.assertIsNone(self.store.get_active_attempt(trip.id))

    def test_profile_reused_and_override_validation(self):
        """자동 ID는 설치별로 유지되고 명시 기기 값은 검증한 뒤 사용"""
        env = {"KORAIL_MOBILE_API_LIVE": "1"}
        with patch("korail_booker.korail.korail.KorailClient") as factory:
            create_live_client(self.store, env)
            first = factory.call_args.args[0]
            create_live_client(TripStore(self.database), env)
            second = factory.call_args.args[0]
        self.assertEqual(
            first.dynapath.token_settings.device_id,
            second.dynapath.token_settings.device_id,
        )
        self.assertRegex(first.dynapath.token_settings.device_id, r"^[0-9a-f]{16}$")
        with self.assertRaises(ValueError):
            create_live_client(
                self.store, env | {"KORAIL_DYNAPATH_DEVICE_ID": "invalid"}
            )

    def test_one_command_questions_save_and_run_without_exports(self):
        """export 없이 질문 답변을 저장하고 발급 ID로 실행까지 연결"""
        responses = [
            "서울",
            "부산",
            (date.today() + timedelta(days=7)).isoformat(),
            "08:00",
            "09:00",
            "",
            "",
            "70000",
            "yes",
            "fixture-account",
        ]
        with (
            patch.dict(os.environ, {"KORAIL_DB_PATH": str(self.database)}, clear=True),
            patch("builtins.input", side_effect=responses),
            patch(
                "korail_booker.app.run_trip",
                side_effect=lambda store, tid, env: store.get_trip(tid),
            ) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["run"]), 0)
        self.assertEqual(self.store.get_trip(1).arrival_station, "부산")
        self.assertEqual(run.call_args.args[1], 1)
        self.assertEqual(run.call_args.args[2]["KORAIL_MAX_FARE_WON"], "70000")
        self.assertEqual(run.call_args.args[2]["KORAIL_REAL_CHARGE_APPROVED"], "1")
        with self.store._connect() as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn("fixture-account", dump)

    def test_declined_confirmation_does_not_run(self):
        """실결제 확인을 거절하면 여행 초안만 저장"""
        trip = self.store.create_trip(make_trip())
        with (
            patch.dict(os.environ, {"KORAIL_DB_PATH": str(self.database)}, clear=True),
            patch("builtins.input", side_effect=["70000", "no"]),
            patch("korail_booker.app.run_trip") as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["run", str(trip.id)]), 0)
        run.assert_not_called()

    def test_s002_backoff_reconnects_but_permanent_failure_does_not(self):
        """조회 S002를 지수 대기로 재접속하고 인증 등 영구 오류는 즉시 전달"""
        trip = self.store.create_trip(make_trip())
        worker, client = Mock(), Mock()
        worker.poll.side_effect = [
            KorailAppError("S002", "busy"),
            KorailAppError("S002", "busy"),
            None,
        ]
        with (
            patch(
                "korail_booker.app.create_live_client", return_value=client
            ) as factory,
            patch("korail_booker.app.create_live_worker", return_value=worker),
            patch("korail_booker.app.clock.sleep") as sleep,
            redirect_stdout(io.StringIO()),
        ):
            run_trip(self.store, trip.id, environment(self.database, live=True))
        self.assertEqual(factory.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60])
        worker.poll.side_effect = KorailAppError("AUTH", "invalid login")
        with (
            patch(
                "korail_booker.app.create_live_client", return_value=client
            ) as factory,
            patch("korail_booker.app.create_live_worker", return_value=worker),
            patch("korail_booker.app.clock.sleep") as sleep,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KorailAppError):
                run_trip(self.store, trip.id, environment(self.database, live=True))
        self.assertEqual(factory.call_count, 1)
        sleep.assert_not_called()

    def test_s002_pre_reserve_recovers_same_candidate_and_pays_once(self):
        """예약 직전 조회 혼잡에서 복구해 같은 열차를 한 번만 결제"""
        trip = self.store.create_trip(make_trip())
        client = client_for_purchase()
        client.get_ticket_list.side_effect = [
            KorailAppError("S002", "busy"),
            korail.BaseKorailResponse(raw={"reservation_list": []}),
            korail.BaseKorailResponse(raw={"tickets": [{"h_pnr_no": "hidden"}]}),
        ]
        with (
            patch("korail_booker.app.create_live_client", return_value=client),
            patch("korail_booker.app.clock.sleep") as sleep,
            redirect_stdout(io.StringIO()),
        ):
            result = run_trip(
                self.store, trip.id, environment(self.database, live=True)
            )
        self.assertEqual(result.status, TripStatus.TICKETED)
        client.reserve.assert_called_once()
        client.pay_with_card.assert_called_once()
        sleep.assert_called_once_with(30)

    def test_reserve_transport_unknown_does_not_restart(self):
        """예약 요청의 통신 결과 불명확은 CLAIMING 유지하고 재예약 금지"""
        trip = self.store.create_trip(make_trip())
        client = client_for_purchase()
        client.reserve.side_effect = KorailTransportError("connection lost")
        with (
            patch("korail_booker.app.create_live_client", return_value=client),
            patch("korail_booker.app.clock.sleep") as sleep,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KorailTransportError):
                run_trip(self.store, trip.id, environment(self.database, live=True))
        client.reserve.assert_called_once()
        client.pay_with_card.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(self.store.get_trip(trip.id).status, TripStatus.CLAIMING)

    def test_paying_reconnect_queries_ticket_without_repayment(self):
        """PAYING 복구 중 S002가 발생해도 발권 조회만 재시도"""
        trip = self.store.create_trip(make_trip())
        self.store.start_trip(trip.id)
        attempt = self.store.claim_candidate(trip.id, make_candidate())
        self.store.start_reservation(attempt.id)
        self.store.mark_reserved(attempt.id, make_reservation())
        self.store.start_payment(attempt.id)
        client = client_for_purchase()
        client.get_ticket_list.side_effect = [
            KorailAppError("S002", "busy"),
            korail.BaseKorailResponse(raw={"tickets": [{"h_pnr_no": "hidden"}]}),
        ]
        with (
            patch("korail_booker.app.create_live_client", return_value=client),
            patch("korail_booker.app.clock.sleep") as sleep,
            redirect_stdout(io.StringIO()),
        ):
            result = run_trip(
                self.store, trip.id, environment(self.database, live=True)
            )
        self.assertEqual(result.status, TripStatus.TICKETED)
        client.reserve.assert_not_called()
        client.pay_with_card.assert_not_called()
        sleep.assert_called_once_with(30)


if __name__ == "__main__":
    unittest.main()
