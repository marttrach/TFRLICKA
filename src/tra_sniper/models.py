from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any


class IdentityType(str, Enum):
    PERSON_ID = "PERSON_ID"
    PASSPORT_NO = "PASSPORT_NO"


class TripType(str, Enum):
    ONEWAY = "ONEWAY"
    ROUNDTRIP = "ROUNDTRIP"


class OrderType(str, Enum):
    BY_TRAIN_NO = "BY_TRAIN_NO"
    BY_TIME = "BY_TIME"


class SeatPreference(str, Enum):
    NONE = "NONE"
    TABLE = "TABLE"


# These labels mirror the official TRC booking form. Keep 23:59: it is an
# actual option even though the regular intervals are every 30 minutes.
BOOKING_TIME_LABELS = tuple(
    f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 30)
) + ("23:59",)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.replace("/", "-"))
    except ValueError as exc:
        raise ValueError(f"Invalid date {value!r}; expected YYYY/MM/DD") from exc


@dataclass(frozen=True, slots=True)
class Leg:
    ride_date: str
    train_numbers: tuple[str, ...] = ()
    start_time: str | None = None
    end_time: str | None = None
    search_by_departure: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Leg:
        return cls(
            ride_date=str(data["ride_date"]),
            train_numbers=tuple(str(item).strip() for item in data.get("train_numbers", [])),
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
            search_by_departure=bool(data.get("search_by_departure", True)),
        )

    def validate(self, order_type: OrderType) -> None:
        ride_date = _parse_date(self.ride_date)
        if ride_date < datetime.now().astimezone().date():
            raise ValueError(f"ride_date {self.ride_date} is in the past")

        if order_type is OrderType.BY_TRAIN_NO:
            if not 1 <= len(self.train_numbers) <= 3:
                raise ValueError("BY_TRAIN_NO requires one to three train_numbers")
            if any(not number.isdigit() for number in self.train_numbers):
                raise ValueError("train_numbers must contain digits only")
        else:
            if not self.start_time or not self.end_time:
                raise ValueError("BY_TIME requires start_time and end_time")
            if self.start_time not in BOOKING_TIME_LABELS:
                raise ValueError(f"Invalid start_time {self.start_time!r}")
            if self.end_time not in BOOKING_TIME_LABELS:
                raise ValueError(f"Invalid end_time {self.end_time!r}")
            if self.start_time >= self.end_time:
                raise ValueError("start_time must precede end_time")


@dataclass(frozen=True, slots=True)
class BookingRequest:
    identity: str
    start_station: str
    end_station: str
    outbound: Leg
    identity_type: IdentityType = IdentityType.PERSON_ID
    trip_type: TripType = TripType.ONEWAY
    order_type: OrderType = OrderType.BY_TRAIN_NO
    quantity: int = 1
    seat_preference: SeatPreference = SeatPreference.NONE
    allow_seat_change: bool = True
    inbound: Leg | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BookingRequest:
        inbound_data = data.get("inbound")
        request = cls(
            identity=str(data["identity"]).strip(),
            identity_type=IdentityType(data.get("identity_type", IdentityType.PERSON_ID)),
            start_station=str(data["start_station"]).strip(),
            end_station=str(data["end_station"]).strip(),
            trip_type=TripType(data.get("trip_type", TripType.ONEWAY)),
            order_type=OrderType(data.get("order_type", OrderType.BY_TRAIN_NO)),
            quantity=int(data.get("quantity", 1)),
            seat_preference=SeatPreference(
                data.get("seat_preference", SeatPreference.NONE)
            ),
            allow_seat_change=bool(data.get("allow_seat_change", True)),
            outbound=Leg.from_dict(data["outbound"]),
            inbound=Leg.from_dict(inbound_data) if inbound_data else None,
        )
        request.validate()
        return request

    def validate(self) -> None:
        if not self.identity:
            raise ValueError("identity is required")
        if not self.start_station or not self.end_station:
            raise ValueError("start_station and end_station are required")
        if self.start_station == self.end_station:
            raise ValueError("start_station and end_station must differ")
        if not 1 <= self.quantity <= 6:
            raise ValueError("quantity must be between 1 and 6")
        if self.trip_type is TripType.ROUNDTRIP and self.inbound is None:
            raise ValueError("ROUNDTRIP requires inbound")
        if self.trip_type is TripType.ONEWAY and self.inbound is not None:
            raise ValueError("ONEWAY must not include inbound")

        self.outbound.validate(self.order_type)
        if self.inbound:
            self.inbound.validate(self.order_type)
            if _parse_date(self.inbound.ride_date) < _parse_date(self.outbound.ride_date):
                raise ValueError("inbound ride_date cannot precede outbound ride_date")

    def redacted(self) -> dict[str, Any]:
        data = asdict(self)
        data["identity"] = "***" + self.identity[-3:] if len(self.identity) >= 3 else "***"
        return data
