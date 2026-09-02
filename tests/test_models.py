from datetime import datetime, timedelta

import pytest

from tra_sniper.models import BookingRequest


def future_date(days: int = 1) -> str:
    return (datetime.now().astimezone().date() + timedelta(days=days)).strftime("%Y/%m/%d")


def valid_data() -> dict:
    return {
        "identity": "TEST-ID",
        "start_station": "1000-臺北",
        "end_station": "3300-臺中",
        "outbound": {"ride_date": future_date(), "train_numbers": ["110"]},
    }


def test_valid_one_way_request() -> None:
    request = BookingRequest.from_dict(valid_data())
    assert request.quantity == 1
    assert request.outbound.train_numbers == ("110",)
    assert request.redacted()["identity"] == "***-ID"


def test_rejects_same_station() -> None:
    data = valid_data()
    data["end_station"] = data["start_station"]
    with pytest.raises(ValueError, match="must differ"):
        BookingRequest.from_dict(data)


def test_roundtrip_requires_inbound() -> None:
    data = valid_data()
    data["trip_type"] = "ROUNDTRIP"
    with pytest.raises(ValueError, match="requires inbound"):
        BookingRequest.from_dict(data)


def test_by_time_requires_range() -> None:
    data = valid_data()
    data["order_type"] = "BY_TIME"
    data["outbound"] = {"ride_date": future_date()}
    with pytest.raises(ValueError, match="requires start_time and end_time"):
        BookingRequest.from_dict(data)


def test_by_time_requires_official_labels_in_order() -> None:
    data = valid_data()
    data["order_type"] = "BY_TIME"
    data["outbound"] = {
        "ride_date": future_date(),
        "start_time": "08:15",
        "end_time": "12:00",
    }
    with pytest.raises(ValueError, match="Invalid start_time"):
        BookingRequest.from_dict(data)

    data["outbound"] = {
        "ride_date": future_date(),
        "start_time": "12:00",
        "end_time": "08:00",
    }
    with pytest.raises(ValueError, match="must precede"):
        BookingRequest.from_dict(data)
