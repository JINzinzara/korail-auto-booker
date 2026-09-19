"""SQLite 저장소"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timezone
from pathlib import Path

from .domain import AttemptStatus, Candidate, PurchaseAttempt, Trip, TripStatus


class TripStore:
    def __init__(self, path: str | Path) -> None:
        """SQLite 파일에 trips, purchase_attempts 데이블 및 단일 활성 claim 인덱스 생성"""
        self.path = str(path)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS trips (
                    id INTEGER PRIMARY KEY,
                    departure_station TEXT NOT NULL,
                    arrival_station TEXT NOT NULL,
                    travel_date TEXT NOT NULL,
                    earliest_departure TEXT NOT NULL,
                    latest_departure TEXT NOT NULL,
                    train_types TEXT NOT NULL,
                    passenger_count INTEGER NOT NULL CHECK (passenger_count > 0),
                    allow_merge_seat INTEGER NOT NULL CHECK (allow_merge_seat IN (0, 1)),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS purchase_attempts (
                    id INTEGER PRIMARY KEY,
                    trip_id INTEGER NOT NULL REFERENCES trips(id),
                    candidate_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reserve_attempted_at TEXT,
                    payment_attempted_at TEXT,
                    UNIQUE (trip_id, candidate_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_trip
                ON purchase_attempts(trip_id)
                WHERE status NOT IN ('TICKETED', 'FAILED');
                """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """외래 키를 활성화하고 transaction 뒤 연결을 항상 종료"""
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create_trip(self, trip: Trip) -> Trip:
        """새 DRAFT 여행을 저장, 발급된 ID를 포함해 반환"""
        if trip.id is not None or trip.status is not TripStatus.DRAFT:
            raise ValueError("only a new draft trip can be created")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trips (
                    departure_station, arrival_station, travel_date,
                    earliest_departure, latest_departure, train_types,
                    passenger_count, allow_merge_seat, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trip.departure_station,
                    trip.arrival_station,
                    trip.travel_date.isoformat(),
                    trip.earliest_departure.isoformat(),
                    trip.latest_departure.isoformat(),
                    json.dumps(trip.train_types, ensure_ascii=False),
                    trip.passenger_count,
                    trip.allow_merge_seat,
                    trip.status.value,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return replace(trip, id=cursor.lastrowid)

    def get_trip(self, trip_id: int) -> Trip | None:
        """ID로 여행을 조회하고 없으면 None"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, departure_station, arrival_station, travel_date,
                       earliest_departure, latest_departure, train_types,
                       passenger_count, allow_merge_seat, status
                FROM trips WHERE id = ?
                """,
                (trip_id,),
            ).fetchone()
        if row is None:
            return None
        return Trip(
            id=row[0],
            departure_station=row[1],
            arrival_station=row[2],
            travel_date=date.fromisoformat(row[3]),
            earliest_departure=time.fromisoformat(row[4]),
            latest_departure=time.fromisoformat(row[5]),
            train_types=tuple(json.loads(row[6])),
            passenger_count=row[7],
            allow_merge_seat=bool(row[8]),
            status=TripStatus(row[9]),
        )

    def get_attempt(self, attempt_id: int) -> PurchaseAttempt | None:
        """ID로 구매 시도를 조회하고 없으면 None"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, trip_id, candidate_key, status, created_at,
                       reserve_attempted_at, payment_attempted_at
                FROM purchase_attempts WHERE id = ?
                """,
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        return PurchaseAttempt(
            id=row[0],
            trip_id=row[1],
            candidate_key=row[2],
            status=AttemptStatus(row[3]),
            created_at=datetime.fromisoformat(row[4]),
            reserve_attempted_at=(
                datetime.fromisoformat(row[5]) if row[5] is not None else None
            ),
            payment_attempted_at=(
                datetime.fromisoformat(row[6]) if row[6] is not None else None
            ),
        )

    def start_trip(self, trip_id: int) -> bool:
        """DRAFT 여행을 MONITORING으로 한 번만 전환"""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE trips SET status = ? WHERE id = ? AND status = ?",
                (TripStatus.MONITORING.value, trip_id, TripStatus.DRAFT.value),
            )
        return cursor.rowcount == 1

    def claim_candidate(
        self, trip_id: int, candidate: Candidate
    ) -> PurchaseAttempt | None:
        """MONITORING 여행의 후보를 원자적으로 한 번만 claim"""
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claimed = connection.execute(
                "UPDATE trips SET status = ? WHERE id = ? AND status = ?",
                (TripStatus.CLAIMING.value, trip_id, TripStatus.MONITORING.value),
            )
            if claimed.rowcount != 1:
                return None
            cursor = connection.execute(
                """
                INSERT INTO purchase_attempts (
                    trip_id, candidate_key, status, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    trip_id,
                    candidate.key,
                    AttemptStatus.CLAIMED.value,
                    now.isoformat(),
                ),
            )
        return PurchaseAttempt(
            id=cursor.lastrowid,
            trip_id=trip_id,
            candidate_key=candidate.key,
            created_at=now,
        )

    def start_reservation(self, attempt_id: int) -> PurchaseAttempt | None:
        """CLAIMED 구매 시도의 예약 호출을 한 번만 시작"""
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.CLAIMED,
            AttemptStatus.RESERVING,
            TripStatus.CLAIMING,
            TripStatus.CLAIMING,
            "reserve_attempted_at",
        )

    def mark_reserved(self, attempt_id: int) -> PurchaseAttempt | None:
        """예약 성공을 기록하고 여행과 구매 시도를 RESERVED로 전환"""
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.RESERVING,
            AttemptStatus.RESERVED,
            TripStatus.CLAIMING,
            TripStatus.RESERVED,
        )

    def retry_after_reservation_failure(
        self, attempt_id: int
    ) -> PurchaseAttempt | None:
        """좌석 확보 실패를 기록하고 여행을 MONITORING으로 복귀"""
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.RESERVING,
            AttemptStatus.FAILED,
            TripStatus.CLAIMING,
            TripStatus.MONITORING,
        )

    def start_payment(self, attempt_id: int) -> PurchaseAttempt | None:
        """RESERVED 구매 시도의 결제 호출을 한 번만 시작"""
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.RESERVED,
            AttemptStatus.PAYING,
            TripStatus.RESERVED,
            TripStatus.PAYING,
            "payment_attempted_at",
        )

    def mark_reconciling(self, attempt_id: int) -> PurchaseAttempt | None:
        """결제 호출 후 승차권 확인이 필요한 상태로 전환"""
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.PAYING,
            AttemptStatus.RECONCILING,
            TripStatus.PAYING,
            TripStatus.RECONCILING,
        )

    def mark_ticketed(self, attempt_id: int) -> PurchaseAttempt | None:
        """승차권 확인이 끝난 여행과 구매 시도를 TICKETED로 전환"""
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.RECONCILING,
            AttemptStatus.TICKETED,
            TripStatus.RECONCILING,
            TripStatus.TICKETED,
        )

    def _transition_attempt(
        self,
        attempt_id: int,
        from_attempt: AttemptStatus,
        to_attempt: AttemptStatus,
        from_trip: TripStatus,
        to_trip: TripStatus,
        timestamp_column: str | None = None,
    ) -> PurchaseAttempt | None:
        """구매 시도와 여행 상태를 한 transaction에서 조건부 전환"""
        if timestamp_column not in {None, "reserve_attempted_at", "payment_attempted_at"}:
            raise ValueError("invalid attempt timestamp column")
        required = {
            AttemptStatus.RESERVING: "AND a.reserve_attempted_at IS NOT NULL",
            AttemptStatus.RESERVED: "AND a.reserve_attempted_at IS NOT NULL",
            AttemptStatus.PAYING: (
                "AND a.reserve_attempted_at IS NOT NULL "
                "AND a.payment_attempted_at IS NOT NULL"
            ),
            AttemptStatus.RECONCILING: (
                "AND a.reserve_attempted_at IS NOT NULL "
                "AND a.payment_attempted_at IS NOT NULL"
            ),
        }.get(from_attempt, "")
        empty_timestamp = (
            f"AND a.{timestamp_column} IS NULL" if timestamp_column is not None else ""
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT a.trip_id
                FROM purchase_attempts AS a
                JOIN trips AS t ON t.id = a.trip_id
                WHERE a.id = ? AND a.status = ? AND t.status = ?
                {required} {empty_timestamp}
                """,
                (attempt_id, from_attempt.value, from_trip.value),
            ).fetchone()
            if row is None:
                return None
            timestamp_assignment = (
                f", {timestamp_column} = ?" if timestamp_column is not None else ""
            )
            attempt_values = (
                (to_attempt.value, now, attempt_id)
                if timestamp_column is not None
                else (to_attempt.value, attempt_id)
            )
            attempt_update = connection.execute(
                f"UPDATE purchase_attempts SET status = ?{timestamp_assignment} "
                "WHERE id = ?",
                attempt_values,
            )
            trip_update = connection.execute(
                "UPDATE trips SET status = ? WHERE id = ?",
                (to_trip.value, row[0]),
            )
            if attempt_update.rowcount != 1 or trip_update.rowcount != 1:
                raise RuntimeError("purchase transition was not atomic")
        return self.get_attempt(attempt_id)
