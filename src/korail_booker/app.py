"""보안(카드, 비밀번호) 입력을 KORAIL 자동 예매 실행으로 연결"""

import argparse
import getpass
import os
import sys
import math
import time as clock
from collections.abc import Mapping, Sequence
from datetime import date, time

from .domain import Trip, TripStatus
from .korail import (
    close_client,
    create_adult_passengers,
    create_card_payment,
    create_live_client,
    create_live_worker,
    login_client,
    is_temporary_error,
)
from .storage import TripStore


def _required(env: Mapping[str, str], name: str) -> str:
    """필수 환경변수를 읽고 빈 값이면 안전하게 중단"""
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: str | None = None) -> int:
    """환경변수를 양의 정수로 변환"""
    value = env.get(name, default)
    if value is None:
        raise ValueError(f"{name} is required")
    try:
        number = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if number < 1:
        raise ValueError(f"{name} must be a positive integer")
    return number


def _approval(env: Mapping[str, str], name: str) -> None:
    """위험 작업 승인 환경변수가 정확히 1인지 확인"""
    if env.get(name) != "1":
        raise PermissionError(f"{name}=1 is required")


def _secret(env: Mapping[str, str], name: str, prompt: str) -> str:
    """환경변수가 없으면 터미널 숨김 입력으로 비밀값을 읽기"""
    value = env.get(name, "").strip() or getpass.getpass(f"{prompt}: ").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _ask(prompt: str, default: str = "") -> str:
    """기본값이 있는 필수 질문을 터미널에 표시"""
    while True:
        value = (
            input(f"{prompt}{f' [{default}]' if default else ''}: ").strip() or default
        )
        if value:
            return value
        print("값을 입력해주세요.")


def interactive_trip(env: Mapping[str, str]) -> Trip:
    """여행 조건을 질문하고 검증한 뒤 저장 가능한 여행으로 변환"""
    values = dict(env)
    fields = (
        ("KORAIL_DEPARTURE_STATION", "출발역", ""),
        ("KORAIL_ARRIVAL_STATION", "도착역", ""),
        ("KORAIL_TRAVEL_DATE", "여행 날짜 YYYY-MM-DD", ""),
        ("KORAIL_EARLIEST_DEPARTURE", "출발 시작시각 HH:MM", ""),
        ("KORAIL_LATEST_DEPARTURE", "출발 종료시각 HH:MM", ""),
        ("KORAIL_TRAIN_TYPES", "열차 종류 (여러 개는 쉼표로 구분)", "KTX"),
        ("KORAIL_PASSENGER_COUNT", "성인 인원", "1"),
    )
    while True:
        for name, label, default in fields:
            values[name] = _ask(label, values.get(name, default))
        try:
            trip = trip_from_env(values)
            if trip.travel_date < date.today():
                raise ValueError("지난 날짜는 선택할 수 없습니다")
            return trip
        except ValueError as error:
            print(f"입력 확인: {error}")


def interactive_run(store: TripStore, trip: Trip, env: Mapping[str, str]) -> Trip:
    """저장 여행과 운임을 확인받아 한 명령으로 실제 감시까지 연결"""
    if trip.status in {TripStatus.TICKETED, TripStatus.STOPPED, TripStatus.FAILED}:
        return trip
    values = dict(env)
    print(
        f"trip_id={trip.id} {trip.departure_station} → {trip.arrival_station} "
        f"{trip.travel_date} {trip.earliest_departure:%H:%M}~{trip.latest_departure:%H:%M} "
        f"성인 {trip.passenger_count}명"
    )
    while True:
        values["KORAIL_MAX_FARE_WON"] = _ask(
            "총 결제 상한 (원)", values.get("KORAIL_MAX_FARE_WON", "")
        )
        try:
            _positive_int(values, "KORAIL_MAX_FARE_WON")
            break
        except ValueError:
            print("운임은 양의 정수로 입력해주세요.")
    if (
        _ask("이 조건으로 자동 예약·실제 카드 결제를 승인합니까? yes/no", "no").lower()
        != "yes"
    ):
        print(
            f"실행하지 않았습니다. 재개: korail-booker run {trip.id}; 초안 중지: korail-booker stop {trip.id}"
        )
        return trip
    values["KORAIL_MEMBER_NO"] = values.get("KORAIL_MEMBER_NO", "").strip() or _ask(
        "KORAIL 회원번호 또는 로그인 ID"
    )
    for name in (
        "KORAIL_MOBILE_API_LIVE",
        "KORAIL_RESERVE_APPROVED",
        "KORAIL_REAL_CHARGE_APPROVED",
    ):
        values[name] = "1"
    # 의존 라이브러리의 HTTP 승인 게이트는 프로세스 환경변수 확인
    previous = os.environ.get("KORAIL_MOBILE_API_LIVE")
    os.environ["KORAIL_MOBILE_API_LIVE"] = "1"
    try:
        return run_trip(store, trip.id, values)
    finally:
        if previous is None:
            os.environ.pop("KORAIL_MOBILE_API_LIVE", None)
        else:
            os.environ["KORAIL_MOBILE_API_LIVE"] = previous


def trip_from_env(env: Mapping[str, str]) -> Trip:
    """비밀정보가 없는 환경변수에서 새 여행 조건을 생성"""
    try:
        travel_date = date.fromisoformat(_required(env, "KORAIL_TRAVEL_DATE"))
        earliest = time.fromisoformat(_required(env, "KORAIL_EARLIEST_DEPARTURE"))
        latest = time.fromisoformat(_required(env, "KORAIL_LATEST_DEPARTURE"))
    except ValueError as error:
        raise ValueError(
            "travel date or departure time has an invalid format"
        ) from error
    train_types = tuple(
        value.strip()
        for value in _required(env, "KORAIL_TRAIN_TYPES").split(",")
        if value.strip()
    )
    return Trip(
        departure_station=_required(env, "KORAIL_DEPARTURE_STATION"),
        arrival_station=_required(env, "KORAIL_ARRIVAL_STATION"),
        travel_date=travel_date,
        earliest_departure=earliest,
        latest_departure=latest,
        train_types=train_types,
        passenger_count=_positive_int(env, "KORAIL_PASSENGER_COUNT", "1"),
    )


def create_trip(store: TripStore, env: Mapping[str, str]) -> Trip:
    """새 여행을 DRAFT로만 저장해 실행 전 고정 ID를 발급"""
    return store.create_trip(trip_from_env(env))


def run_trip(store: TripStore, trip_id: int, env: Mapping[str, str]) -> Trip:
    """저장된 여행 하나를 명시적 예약·실결제 승인으로 실행 또는 복구"""
    trip = store.get_trip(trip_id)
    if trip is None:
        raise ValueError(f"trip {trip_id} does not exist")
    if trip.status in {TripStatus.TICKETED, TripStatus.STOPPED, TripStatus.FAILED}:
        return trip
    if trip.status is TripStatus.CLAIMING:
        raise RuntimeError("CLAIMING trip requires manual reconciliation")

    _approval(env, "KORAIL_MOBILE_API_LIVE")
    _approval(env, "KORAIL_RESERVE_APPROVED")
    _approval(env, "KORAIL_REAL_CHARGE_APPROVED")
    max_fare_won = _positive_int(env, "KORAIL_MAX_FARE_WON")
    interval_seconds = float(env.get("KORAIL_POLL_INTERVAL_SECONDS", "10"))
    if not math.isfinite(interval_seconds) or interval_seconds < 5:
        raise ValueError("KORAIL_POLL_INTERVAL_SECONDS must be at least 5")
    max_polls = (
        _positive_int(env, "KORAIL_MAX_POLLS")
        if env.get("KORAIL_MAX_POLLS") is not None
        else None
    )
    passengers = create_adult_passengers(trip.passenger_count)
    card = create_card_payment(
        _secret(env, "KORAIL_CARD_NUMBER", "카드번호"),
        _secret(env, "KORAIL_CARD_PASSWORD", "카드 비밀번호 앞 2자리"),
        _secret(env, "KORAIL_CARD_EXPIRE", "카드 유효기간 YYMM"),
        _secret(env, "KORAIL_CARD_BIRTHDAY", "생년월일 YYMMDD"),
    )
    member_no = _required(env, "KORAIL_MEMBER_NO")
    password = _secret(env, "KORAIL_PASSWORD", "코레일 비밀번호")

    max_restarts = _positive_int(env, "KORAIL_MAX_RESTARTS", "5")
    restarts = 0
    while True:
        client = None
        delay = None
        try:
            client = create_live_client(store, env)
            login_client(client, member_no, password)
            current = store.get_trip(trip_id)
            if current.status is TripStatus.DRAFT and not store.start_trip(trip_id):
                raise RuntimeError("trip could not start")
            print(
                f"trip_id={trip_id} status={store.get_trip(trip_id).status.value}",
                flush=True,
            )
            worker = create_live_worker(
                store,
                client,
                passengers,
                card,
                max_fare_won,
                reserve_approved=True,
                real_charge_approved=True,
            )
            worker.poll(trip_id, interval_seconds=interval_seconds, max_polls=max_polls)
            break
        except Exception as error:
            current = store.get_trip(trip_id)
            safe = current is not None and current.status in {
                TripStatus.DRAFT,
                TripStatus.MONITORING,
                TripStatus.RECONCILING,
                TripStatus.PAYING,
            }
            if not safe or not is_temporary_error(error) or restarts >= max_restarts:
                raise
            delay = min(30 * 2 ** min(restarts, 4), 300)
            restarts += 1
            print(
                f"일시적 서버 오류: {delay}초 후 재접속 ({restarts}/{max_restarts}), "
                f"trip_id={trip_id} status={current.status.value}",
                flush=True,
            )
        finally:
            if client is not None:
                close_client(client)
        clock.sleep(delay)
    result = store.get_trip(trip_id)
    if result is None:
        raise RuntimeError("trip disappeared during execution")
    return result


def _parser() -> argparse.ArgumentParser:
    """중복 예약을 막는 create와 run 명령 파서를 생성"""
    parser = argparse.ArgumentParser(prog="korail-booker")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("create", help="save a DRAFT trip without network access")
    run = commands.add_parser("run", help="run or resume one saved trip")
    run.add_argument("trip_id", type=int, nargs="?")
    commands.add_parser("start", help="ask travel questions and start monitoring")
    stop = commands.add_parser("stop", help="stop a draft or monitoring trip")
    stop.add_argument("trip_id", type=int)
    status = commands.add_parser("status", help="show saved trips")
    status.add_argument("trip_id", type=int, nargs="?")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """명령을 실행하고 비밀정보 없는 여행 ID와 상태만 출력"""
    args = _parser().parse_args(argv)
    try:
        store = TripStore(os.environ.get("KORAIL_DB_PATH", "korail-booker.sqlite3"))
        if args.command == "status":
            for trip in store.list_trips(args.trip_id):
                print(
                    f"trip_id={trip.id} status={trip.status.value} "
                    f"{trip.departure_station}→{trip.arrival_station} {trip.travel_date} "
                    f"{trip.earliest_departure:%H:%M}~{trip.latest_departure:%H:%M}"
                )
            return 0
        if args.command == "stop":
            if not store.stop_trip(args.trip_id):
                raise ValueError("DRAFT 또는 MONITORING 여행만 중지할 수 있습니다")
            trip = store.get_trip(args.trip_id)
        elif args.command == "create":
            trip = create_trip(store, os.environ)
        elif args.command == "run" and args.trip_id is not None:
            trip = store.get_trip(args.trip_id)
            if trip is None:
                raise ValueError(f"trip {args.trip_id} does not exist")
            if any(
                os.environ.get(name)
                for name in ("KORAIL_RESERVE_APPROVED", "KORAIL_REAL_CHARGE_APPROVED")
            ):
                trip = run_trip(store, args.trip_id, os.environ)
            else:
                trip = interactive_run(store, trip, os.environ)
        else:
            trip = store.create_trip(interactive_trip(os.environ))
            trip = interactive_run(store, trip, os.environ)
    except (KeyboardInterrupt, EOFError):
        print(
            "중단되었습니다. status로 확인한 뒤 같은 trip_id로 재개하세요.",
            file=sys.stderr,
        )
        return 130
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"trip_id={trip.id} status={trip.status.value}")
    return 0
