"""SQLite 저장소"""

import json
import sqlite3
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

    def _connect(self) -> sqlite3.Connection:
        """외래 키 검사를 활성화한 SQLite 연결 생성"""
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

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
