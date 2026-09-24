"""SQLite 저장소"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timezone
from pathlib import Path

from .domain import (
    AttemptStatus,
    Candidate,
    PurchaseAttempt,
    Reservation,
    Trip,
    TripStatus,
    normalize_station,
)


class TripStore:
    def __init__(self, path: str | Path) -> None:
        """SQLite 파일에 여행·구매 테이블과 단일 활성 claim 인덱스 생성"""
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
                    reservation_json TEXT,
                    UNIQUE (trip_id, candidate_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_trip
                ON purchase_attempts(trip_id)
                WHERE status NOT IN ('TICKETED', 'FAILED');
                CREATE TABLE IF NOT EXISTS device_profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    device_id TEXT NOT NULL,
                    os_version TEXT NOT NULL,
                    device_model TEXT NOT NULL
                );
                """)
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(purchase_attempts)")
            }
            if "reservation_json" not in columns:
                connection.execute(
                    "ALTER TABLE purchase_attempts ADD COLUMN reservation_json TEXT"
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """외래 키를 활성화하고 transaction 뒤 연결을 항상 종료"""
        connection = sqlite3.connect(self.path)
        connection.create_function("station", 1, normalize_station, deterministic=True)
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
            connection.execute("BEGIN IMMEDIATE")
            self._check_conflict(connection, trip)
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

    def _check_conflict(self, connection: sqlite3.Connection, trip: Trip) -> None:
        """동일 방향의 전후 2일 내 초안·활성·발권 여행을 원자적으로 차단"""
        row = connection.execute(
            """
            SELECT id FROM trips
            WHERE station(departure_station) = station(?)
              AND station(arrival_station) = station(?)
              AND abs(julianday(travel_date) - julianday(?)) <= 2
              AND status NOT IN ('FAILED', 'STOPPED')
              AND id != ? LIMIT 1
            """,
            (
                trip.departure_station,
                trip.arrival_station,
                trip.travel_date.isoformat(),
                trip.id or 0,
            ),
        ).fetchone()
        if row is not None:
            raise ValueError(f"같은 방향 ±2일 내 여행이 있습니다: trip_id={row[0]}")

    def device_profile(self, defaults: tuple[str, str, str]) -> tuple[str, str, str]:
        """설치별 비밀정보 없는 기기 프로필을 최초 한 번 저장하고 재사용"""
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO device_profile VALUES (1, ?, ?, ?)", defaults
            )
            return connection.execute(
                "SELECT device_id, os_version, device_model FROM device_profile WHERE id = 1"
            ).fetchone()

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

    def list_trips(self, trip_id: int | None = None) -> list[Trip]:
        """저장된 여행 ID와 상태를 확인할 목록 반환"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM trips WHERE ? IS NULL OR id = ? ORDER BY id DESC",
                (trip_id, trip_id),
            ).fetchall()
        return [self.get_trip(row[0]) for row in rows]

    def get_attempt(self, attempt_id: int) -> PurchaseAttempt | None:
        """ID로 구매 시도를 조회하고 없으면 None"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, trip_id, candidate_key, status, created_at,
                        reserve_attempted_at, payment_attempted_at, reservation_json
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
            reservation=_reservation_from_json(row[7]),
        )

    def get_active_attempt(self, trip_id: int) -> PurchaseAttempt | None:
        """여행의 완료되지 않은 구매 시도를 조회하고 없으면 None"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM purchase_attempts
                WHERE trip_id = ? AND status NOT IN (?, ?)
                ORDER BY id DESC LIMIT 1
                """,
                (
                    trip_id,
                    AttemptStatus.TICKETED.value,
                    AttemptStatus.FAILED.value,
                ),
            ).fetchone()
        return self.get_attempt(row[0]) if row is not None else None

    def start_trip(self, trip_id: int) -> bool:
        """DRAFT 여행을 MONITORING으로 한 번만 전환"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            trip = self.get_trip(trip_id)
            if trip is None:
                return False
            self._check_conflict(connection, trip)
            cursor = connection.execute(
                "UPDATE trips SET status = ? WHERE id = ? AND status = ?",
                (TripStatus.MONITORING.value, trip_id, TripStatus.DRAFT.value),
            )
        return cursor.rowcount == 1

    def stop_trip(self, trip_id: int) -> bool:
        """아직 구매를 시작하지 않은 초안 또는 감시 여행을 중지"""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE trips SET status = ? WHERE id = ? AND status IN ('DRAFT', 'MONITORING')",
                (TripStatus.STOPPED.value, trip_id),
            )
        return cursor.rowcount == 1

    def claim_candidate(
        self, trip_id: int, candidate: Candidate
    ) -> PurchaseAttempt | None:
        """MONITORING 여행의 후보를 원자적으로 한 번만 claim"""
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            trip = self.get_trip(trip_id)
            if trip is None:
                return None
            self._check_conflict(connection, trip)
            duplicate = connection.execute(
                """
                SELECT 1 FROM purchase_attempts
                WHERE trip_id = ? AND candidate_key = ?
                """,
                (trip_id, candidate.key),
            ).fetchone()
            if duplicate is not None:
                return None
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

    def release_unsent_reservation(self, attempt_id: int) -> None:
        """예약 미전송이 확인된 claim만 해제해 같은 후보의 재조회를 허용"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT trip_id FROM purchase_attempts WHERE id = ? "
                "AND status = 'RESERVING' AND reservation_json IS NULL "
                "AND payment_attempted_at IS NULL",
                (attempt_id,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "DELETE FROM purchase_attempts WHERE id = ?", (attempt_id,)
                )
                connection.execute(
                    "UPDATE trips SET status = 'MONITORING' WHERE id = ? AND status = 'CLAIMING'",
                    row,
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

    def mark_reserved(
        self, attempt_id: int, reservation: Reservation
    ) -> PurchaseAttempt | None:
        """예약 성공을 기록하고 여행과 구매 시도를 RESERVED로 전환"""
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.RESERVING,
            AttemptStatus.RESERVED,
            TripStatus.CLAIMING,
            TripStatus.RESERVED,
            reservation=reservation,
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

    def fail_payment(self, attempt_id: int) -> PurchaseAttempt | None:
        """확정된 결제 실패를 기록하고 여행과 구매 시도를 FAILED로 전환"""
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.PAYING,
            AttemptStatus.FAILED,
            TripStatus.PAYING,
            TripStatus.FAILED,
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
        reservation: Reservation | None = None,
    ) -> PurchaseAttempt | None:
        """구매 시도와 여행 상태를 transaction에서 조건부 전환"""
        if timestamp_column not in {
            None,
            "reserve_attempted_at",
            "payment_attempted_at",
        }:
            raise ValueError("invalid attempt timestamp column")
        if (to_attempt is AttemptStatus.RESERVED) != (reservation is not None):
            raise ValueError("reserved transition requires reservation data")
        required = {
            AttemptStatus.RESERVING: "AND a.reserve_attempted_at IS NOT NULL",
            AttemptStatus.RESERVED: (
                "AND a.reserve_attempted_at IS NOT NULL "
                "AND a.reservation_json IS NOT NULL"
            ),
            AttemptStatus.PAYING: (
                "AND a.reserve_attempted_at IS NOT NULL "
                "AND a.payment_attempted_at IS NOT NULL "
                "AND a.reservation_json IS NOT NULL"
            ),
            AttemptStatus.RECONCILING: (
                "AND a.reserve_attempted_at IS NOT NULL "
                "AND a.payment_attempted_at IS NOT NULL "
                "AND a.reservation_json IS NOT NULL"
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
            assignments = ["status = ?"]
            attempt_values: list[object] = [to_attempt.value]
            if timestamp_column is not None:
                assignments.append(f"{timestamp_column} = ?")
                attempt_values.append(now)
            if reservation is not None:
                assignments.append("reservation_json = ?")
                attempt_values.append(_reservation_json(reservation))
            attempt_values.append(attempt_id)
            attempt_update = connection.execute(
                f"UPDATE purchase_attempts SET {', '.join(assignments)} WHERE id = ?",
                attempt_values,
            )
            trip_update = connection.execute(
                "UPDATE trips SET status = ? WHERE id = ?",
                (to_trip.value, row[0]),
            )
            if attempt_update.rowcount != 1 or trip_update.rowcount != 1:
                raise RuntimeError("purchase transition was not atomic")
        return self.get_attempt(attempt_id)


def _reservation_json(reservation: Reservation) -> str:
    """내부 예약 복구값을 SQLite 저장용 JSON으로 직렬화"""
    return json.dumps(
        {
            "reference": reservation.reference,
            "amount": reservation.amount,
            "window_no": reservation.window_no,
            "job_sequence_1": reservation.job_sequence_1,
            "job_sequence_2": reservation.job_sequence_2,
            "change_no": reservation.change_no,
        },
        separators=(",", ":"),
    )


def _reservation_from_json(value: str | None) -> Reservation | None:
    """SQLite JSON을 결제 재개용 내부 예약으로 복원"""
    return Reservation(**json.loads(value)) if value is not None else None
