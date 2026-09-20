"""실행 명령의 여행 생성·실행과 비밀정보 비노출 확인"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from korail_booker.app import _secret, main
from korail_booker.domain import TripStatus
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


def environment(database: Path, *, live: bool = False) -> dict[str, str]:
    """DRAFT 생성 또는 모의 라이브 실행용 환경변수 생성"""
    values = {
        "KORAIL_DB_PATH": str(database),
        "KORAIL_DEPARTURE_STATION": "서울",
        "KORAIL_ARRIVAL_STATION": "부산",
        "KORAIL_TRAVEL_DATE": "2026-10-01",
        "KORAIL_EARLIEST_DEPARTURE": "08:00",
        "KORAIL_LATEST_DEPARTURE": "12:00",
        "KORAIL_TRAIN_TYPES": "KTX",
        "KORAIL_PASSENGER_COUNT": "2",
    }
    if live:
        values |= {
            "KORAIL_MOBILE_API_LIVE": "1",
            "KORAIL_RESERVE_APPROVED": "1",
            "KORAIL_REAL_CHARGE_APPROVED": "1",
            "KORAIL_MAX_FARE_WON": "120000",
            "KORAIL_POLL_INTERVAL_SECONDS": "5",
            "KORAIL_MAX_POLLS": "1",
            "KORAIL_MEMBER_NO": "01012345678",
            "KORAIL_PASSWORD": "account-secret",
            "KORAIL_CARD_NUMBER": "4111111111111111",
            "KORAIL_CARD_PASSWORD": "12",
            "KORAIL_CARD_EXPIRE": "2912",
            "KORAIL_CARD_BIRTHDAY": "900101",
        }
    return values


class ApplicationTest(unittest.TestCase):
    """진입점이 중복 실행과 비밀정보 노출을 막는지 확인"""

    def test_create_then_run_saved_trip_without_printing_secrets(self) -> None:
        """고정 ID를 생성한 뒤 같은 여행만 실행하고 비밀값은 숨기는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            with patch.dict(os.environ, environment(database), clear=True):
                self.assertEqual(main(["create"]), 0)
            client, worker, output = Mock(), Mock(), io.StringIO()
            with (
                patch.dict(os.environ, environment(database, live=True), clear=True),
                patch("korail_booker.app.create_live_client", return_value=client),
                patch("korail_booker.app.create_live_worker", return_value=worker),
                redirect_stdout(output),
            ):
                result = main(["run", "1"])
            trip = TripStore(database).get_trip(1)

        self.assertEqual(result, 0)
        self.assertEqual(trip.status, TripStatus.MONITORING)
        worker.poll.assert_called_once_with(1, interval_seconds=5.0, max_polls=1)
        client.logout.assert_called_once_with()
        client.close.assert_called_once_with()
        printed = output.getvalue()
        for secret in ("01012345678", "account-secret", "4111111111111111"):
            self.assertNotIn(secret, printed)
        show_flow(
            "실행 진입점",
            "입력: create 후 trip_id=1 run, 예약·실결제 승인",
            "흐름: 로그인 → polling worker → 세션·연결 종료",
            f"출력: {printed.strip()}",
            "계정·비밀번호·카드정보 출력: 없음",
        )

    def test_run_blocks_before_network_without_charge_approval(self) -> None:
        """실결제 승인이 없으면 client 생성 전 실행을 차단하는지 확인"""
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            with patch.dict(os.environ, environment(database), clear=True):
                self.assertEqual(main(["create"]), 0)
            values = environment(database, live=True)
            values.pop("KORAIL_REAL_CHARGE_APPROVED")
            errors = io.StringIO()
            with (
                patch.dict(os.environ, values, clear=True),
                patch("korail_booker.app.create_live_client") as create_client,
                redirect_stderr(errors),
            ):
                result = main(["run", "1"])

        self.assertEqual(result, 2)
        create_client.assert_not_called()
        self.assertIn("KORAIL_REAL_CHARGE_APPROVED=1", errors.getvalue())
        show_flow(
            "실결제 승인 차단",
            "입력: 실결제 승인 없음",
            "출력: client 생성·로그인·예약·결제 0회",
        )

    def test_secret_uses_environment_or_hidden_prompt(self) -> None:
        """비밀값을 환경변수에서 우선 읽고 없으면 숨김 입력하는지 확인"""
        with patch(
            "korail_booker.app.getpass.getpass", return_value="prompt-secret"
        ) as prompt:
            from_prompt = _secret({}, "SECRET", "비밀값")
            from_environment = _secret(
                {"SECRET": "environment-secret"}, "SECRET", "비밀값"
            )

        self.assertEqual(from_prompt, "prompt-secret")
        self.assertEqual(from_environment, "environment-secret")
        prompt.assert_called_once_with("비밀값: ")
        show_flow(
            "운영체제 독립 비밀 입력",
            "입력: 환경변수 또는 터미널 숨김 입력",
            "출력: 메모리에서만 사용하는 비밀번호·카드값",
            "파일·Keychain 저장: 없음",
        )
