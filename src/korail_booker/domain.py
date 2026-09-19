"""외부 API와 분리된 여행 및 구매 시도 계약"""

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum


class TripStatus(StrEnum):
    DRAFT = "DRAFT"
    MONITORING = "MONITORING"
    CLAIMING = "CLAIMING"
    RESERVED = "RESERVED"
    PAYING = "PAYING"
    RECONCILING = "RECONCILING"
    TICKETED = "TICKETED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class AttemptStatus(StrEnum):
    CLAIMED = "CLAIMED"
    RESERVING = "RESERVING"
    RESERVED = "RESERVED"
    PAYING = "PAYING"
    RECONCILING = "RECONCILING"
    TICKETED = "TICKETED"
    FAILED = "FAILED"


class SeatOption(StrEnum):
    FULL = "FULL"
    MERGE = "MERGE"


@dataclass(frozen=True, slots=True)
class Trip:
    departure_station: str
    arrival_station: str
    travel_date: date
    earliest_departure: time
    latest_departure: time
    train_types: tuple[str, ...]
    passenger_count: int = 1
    allow_merge_seat: bool = False
    status: TripStatus = TripStatus.DRAFT
    id: int | None = None

    def __post_init__(self) -> None:
        """여행 입력값과 초기 상태의 불변조건 검증"""
        if not isinstance(self.departure_station, str) or not isinstance(
            self.arrival_station, str
        ):
            raise ValueError("stations must be strings")
        if type(self.travel_date) is not date:
            raise ValueError("travel date must be a date")
        if (
            type(self.earliest_departure) is not time
            or type(self.latest_departure) is not time
        ):
            raise ValueError("departure bounds must be times")
        if not isinstance(self.train_types, tuple) or any(
            not isinstance(value, str) for value in self.train_types
        ):
            raise ValueError("train types must be a tuple of strings")
        if type(self.passenger_count) is not int:
            raise ValueError("passenger count must be an integer")
        if type(self.allow_merge_seat) is not bool:
            raise ValueError("allow merge seat must be a boolean")
        if not isinstance(self.status, TripStatus):
            raise ValueError("invalid trip status")
        if self.id is not None and type(self.id) is not int:
            raise ValueError("trip id must be an integer")
        if not self.departure_station.strip() or not self.arrival_station.strip():
            raise ValueError("departure and arrival stations are required")
        if self.departure_station == self.arrival_station:
            raise ValueError("departure and arrival stations must differ")
        if self.earliest_departure > self.latest_departure:
            raise ValueError("earliest departure must not be after latest departure")
        if not self.train_types or any(not value.strip() for value in self.train_types):
            raise ValueError("at least one non-empty train type is required")
        if self.passenger_count < 1:
            raise ValueError("passenger count must be positive")
        if self.id is not None and self.id < 1:
            raise ValueError("trip id must be positive")


@dataclass(frozen=True, slots=True)
class Candidate:
    train_no: str
    train_type: str
    departure_at: datetime
    arrival_at: datetime
    seat_option: SeatOption

    def __post_init__(self) -> None:
        """열차 후보의 식별값, 시각, 좌석 조건 검증"""
        if not isinstance(self.train_no, str) or not isinstance(self.train_type, str):
            raise ValueError("train number and type must be strings")
        if (
            type(self.departure_at) is not datetime
            or type(self.arrival_at) is not datetime
        ):
            raise ValueError("candidate times must be datetimes")
        if not isinstance(self.seat_option, SeatOption):
            raise ValueError("invalid seat option")
        if not self.train_no.strip() or not self.train_type.strip():
            raise ValueError("train number and type are required")
        if (
            self.departure_at.utcoffset() is not None
            or self.arrival_at.utcoffset() is not None
        ):
            raise ValueError("candidate times must be local naive datetimes")
        if self.arrival_at <= self.departure_at:
            raise ValueError("arrival must be after departure")

    @property
    def key(self) -> str:
        """열차번호와 출발시각으로 후보 식별자 생성"""
        return f"{self.train_no}:{self.departure_at.isoformat(timespec='minutes')}"


@dataclass(frozen=True, slots=True)
class PurchaseAttempt:
    trip_id: int
    candidate_key: str
    status: AttemptStatus = AttemptStatus.CLAIMED
    id: int | None = None
    created_at: datetime | None = None
    reserve_attempted_at: datetime | None = None
    payment_attempted_at: datetime | None = None

    def __post_init__(self) -> None:
        """구매 시도 식별값과 예약·결제 순서의 불변조건 검증"""
        if type(self.trip_id) is not int:
            raise ValueError("trip id must be an integer")
        if not isinstance(self.candidate_key, str):
            raise ValueError("candidate key must be a string")
        if not isinstance(self.status, AttemptStatus):
            raise ValueError("invalid attempt status")
        if self.id is not None and type(self.id) is not int:
            raise ValueError("attempt id must be an integer")
        if self.trip_id < 1:
            raise ValueError("trip id must be positive")
        if not self.candidate_key.strip():
            raise ValueError("candidate key is required")
        if self.id is not None and self.id < 1:
            raise ValueError("attempt id must be positive")
        if self.payment_attempted_at is not None and self.reserve_attempted_at is None:
            raise ValueError("payment requires a recorded reservation attempt")
