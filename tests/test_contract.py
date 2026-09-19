"""고정된 KORAIL 라이브러리의 필수 공개 계약 확인"""

import inspect
import tomllib
import unittest
from pathlib import Path

import korail_mobile_api as korail

PINNED_COMMIT = (
    "e798fbcf08e56297b4d16a5c300bd7a4f167722d"  # dependencies = korail_mobile_api
)


class ContractTest(unittest.TestCase):
    """업데이트로 핵심 API의 변경 감지"""

    def test_dependency_pin(self) -> None:
        """설치 대상 커밋과 라이브러리 버전이 고정됐는지 확인한다."""
        project = tomllib.loads(Path("pyproject.toml").read_text())
        dependency = project["project"]["dependencies"][0]
        self.assertTrue(dependency.endswith(PINNED_COMMIT))
        self.assertEqual(korail.__version__, "1.1.1")

    def test_public_api(self) -> None:
        """로그인부터 발권 확인까지 필요한 공개 메서드 확인"""
        methods = {
            "login",
            "logout",
            "search_trains",
            "get_train_schedule",
            "get_price_fare_quote",
            "reserve",
            "reserve_merge",
            "cancel_unpaid_hold",
            "pay_with_card",
            "get_reservation_history",
            "get_ticket_list",
            "get_ticket_reservation_detail",
        }
        self.assertFalse(methods - set(dir(korail.KorailClient)))

    def test_consent_required(self) -> None:
        """모든 상태 변경 메서드의 명시적 키워드 동의 요구 확인"""
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
            self.assertIs(consent.default, inspect.Parameter.empty)

    def test_mutation_guard(self) -> None:
        """기본 동의 객체의 모든 상태 변경 차단 확인"""
        consent = korail.MutationConsent()
        self.assertTrue(consent.dry_run)
        self.assertTrue(consent.fake_card_only)
        self.assertFalse(consent.real_card_acknowledged)
        for category in ("reserve", "payment", "cancel", "refund"):
            with self.assertRaises(korail.KorailMutationNotAllowedError):
                korail.require_mutation_consent(consent, category)

    def test_seat_contract(self) -> None:
        """전 구간 좌석과 좌석+입석 예약 코드 확인"""
        self.assertEqual(korail.KorailReservationJobType.IMMEDIATE.value, "1101")
        self.assertEqual(korail.KorailReservationJobType.MERGE_STANDING.value, "1202")
        self.assertEqual(
            korail.KORAIL_MERGE_SEAT_FLAGS_BY_CABIN,
            {"1": frozenset({"A", "G"}), "2": frozenset({"A", "S"})},
        )

    def test_result_contract(self) -> None:
        """운임부터 좌석 배정까지 필요한 응답 필드 확인"""
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
                "payment_deadline_date",
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
            korail.ReservationSeatDetail: {
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
