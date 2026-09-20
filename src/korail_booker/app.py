"""보안(카드, 비밀번호) 입력을 KORAIL 자동 예매 실행으로 연결"""

import argparse
import os
import sys
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
    if interval_seconds < 5:
        raise ValueError("KORAIL_POLL_INTERVAL_SECONDS must be at least 5")
    max_polls = (
        _positive_int(env, "KORAIL_MAX_POLLS")
        if env.get("KORAIL_MAX_POLLS") is not None
        else None
    )
    passengers = create_adult_passengers(trip.passenger_count)
    card = create_card_payment(
        _required(env, "KORAIL_CARD_NUMBER"),
        _required(env, "KORAIL_CARD_PASSWORD"),
        _required(env, "KORAIL_CARD_EXPIRE"),
        _required(env, "KORAIL_CARD_BIRTHDAY"),
    )
    member_no = _required(env, "KORAIL_MEMBER_NO")
    password = _required(env, "KORAIL_PASSWORD")

    client = create_live_client()
    try:
        login_client(client, member_no, password)
        if trip.status is TripStatus.DRAFT and not store.start_trip(trip_id):
            raise RuntimeError("trip could not start")
        worker = create_live_worker(
            store,
            client,
            passengers,
            card,
            max_fare_won,
            reserve_approved=True,
            real_charge_approved=True,
        )
        worker.poll(
            trip_id,
            interval_seconds=interval_seconds,
            max_polls=max_polls,
        )
    finally:
        close_client(client)
    result = store.get_trip(trip_id)
    if result is None:
        raise RuntimeError("trip disappeared during execution")
    return result


def _parser() -> argparse.ArgumentParser:
    """중복 예약을 막는 create와 run 명령 파서를 생성"""
    parser = argparse.ArgumentParser(prog="korail-booker")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="save a DRAFT trip without network access")
    run = commands.add_parser("run", help="run or resume one saved trip")
    run.add_argument("trip_id", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """명령을 실행하고 비밀정보 없는 여행 ID와 상태만 출력"""
    args = _parser().parse_args(argv)
    store = TripStore(os.environ.get("KORAIL_DB_PATH", "korail-booker.sqlite3"))
    try:
        trip = (
            create_trip(store, os.environ)
            if args.command == "create"
            else run_trip(store, args.trip_id, os.environ)
        )
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"trip_id={trip.id} status={trip.status.value}")
    return 0
