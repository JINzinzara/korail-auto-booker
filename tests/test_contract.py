"""KORAIL 라이브러리의 필수 계약 확인"""

import inspect
import tomllib
import unittest
from pathlib import Path

import korail_mobile_api as korail

PINNED_COMMIT = (
    "e798fbcf08e56297b4d16a5c300bd7a4f167722d"  # dependencies = korail_mobile_api
)


class ContractTest(unittest.TestCase):
    """업데이트로 핵심 API가 변경되는 것을 감지"""

    def test_dependency_pin(self) -> None:
        # 설치 대상 commit과 라이브러리의 버전 고정
        project = tomllib.loads(Path("pyproject.toml").read_text())
        dependency = project["project"]["dependencies"][0]
        self.assertTrue(dependency.endswith(PINNED_COMMIT))
        self.assertEqual(korail.__version__, "1.1.1")

    def test_public_api(self) -> None:
        # 로그인부터 발권 확인까지 필요한 공개 매서드 고정
        methods = {
            "login",
            "logout",
            "search_trains",
            "get_train_schedule",
            "get_price_fare_quote",
            "reserve",
            "reserve_merge",
            "cancel_unpaid_hold",
            "pay_with card",
            "get_reservation_history",
            "get_ticket_list",
            "get_ticket_reservation_detail",
        }
        self.assertFalse(methods - set(dir(korail.KorailClient)))

    def test_consent_required(self) -> None:
        # 모든 상태 변경을 키워드 동의 필요
        methods = (
            "reserve",
            "reserve_merge",
            "cancel_unpaid_hold",
            "pay_with_card",
        )
        for method in methods:
            parameters = inspect.signature(
                getattr(korail.KorailClient, method)
            ).parameters
            self.assertIn("consent", parameters)
            consent = parameters["consent"]
            self.assertEqual(consent.kind, inspect.Parameter.KEYWORD_ONLY)
            self.assertIn(consent.default, inspect.Parameter.empty)

    def test_mutation_guard(self) -> None:
        # 기본 동의로는 어떤 상태 변경도 불가능
        consent = korail.MutationConsent()
        self.assertTrue(consent.dry_run)
        self.assertTrue(consent.fake_card_only)
        self.assertFalse(consent.real_card_acknowledged)
        for category in ("reserve", "payment", "cancel", "refund"):
            with self.assertRaises(korail.KorailMutationNotAllowedError):
                korail.require_mutation_consent(consent, category)

    def test_seat_contract(self) -> None:
        # 일반 좌석과 좌석 + 입석 예약 코드 고정
        self.assertEqual(korail.KorailResercationJobType.IMMEDIATE.vlaue, "1101")
        self.assertEqual(korail.KorailReservationJobType.MERGE_STANDING.value, "1202")
        self.assertEqual(
            korail.KORAIL_MERGE_FLAGS_BY_CABIN,
            {"1": frozenset({"A", "G"}), "2": frozenset({"A", "S"})},
        )

    def test_result_contract(self) -> None:
        # 운임 확인부터 좌석 배정 검증까지 사용하는 응답 필드 고정
        contracts = {
            korail.PriceFareQuoteResponse: {"fares"},
            korail.PriceFare: {
                "received_fare",
                "received_price",
                "total_amount",
            },
            korail.ReservationHoldResponse: {
                "pnr_no",
                "received_amount",
                "payment_deadline_data",
                "payment_deadline_time",
                "journeys",
            },
            korail.TicketReservationDetailResponse: {
                "pnr_no",
                "total_received_amount",
                "payment_flag",
                "journeys",
            },
            korail.ReservationDetailJourney: {
                "departure_station_name",
                "arrival_station_name",
                "train_no",
                "seats",
            },
            korail.ResercationSeatDetail: {
                "car_no",
                "seat_no",
                "room_class_name",
                "received_amount",
                "seat_group_name",
            },
        }
        for model, fields in contracts.items():
            self.assertFalse(fields - set(model.__dataclass_fields__))


if __name__ == "__main__":
    unittest.main()
