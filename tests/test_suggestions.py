from __future__ import annotations

from datetime import UTC, datetime

from tra_sniper.suggestions import (
    TAIPEI,
    TrainCandidate,
    candidates_from_records,
    minutes_already_past,
    pair_transfers,
    sort_candidates,
)


def record(no: str, train_type: str, code: str, departure: str, arrival: str) -> dict:
    return {
        "TrainInfo": {
            "TrainNo": no,
            "TrainTypeCode": code,
            "TrainTypeName": {"Zh_tw": train_type},
        },
        "StopTimes": [
            {"DepartureTime": departure},
            {"ArrivalTime": arrival},
        ],
    }


def candidate(no: str, departure: str, arrival: str, duration: int) -> TrainCandidate:
    return TrainCandidate(no, "自強", departure, arrival, duration, True, "對號列車", "", True)


def test_classification_and_sorting_follow_requested_weights() -> None:
    values = candidates_from_records(
        [
            record("A", "區間", "6", "08:30", "10:00"),
            record("B", "自強", "3", "08:45", "09:45"),
            record("C", "自強", "3", "07:30", "08:40"),
        ],
        "08:00",
        "09:00",
    )
    ranked = sort_candidates(values, "08:00", "09:00", prefer_reserved=True)
    assert [item.train_no for item in ranked] == ["B", "A", "C"]
    assert ranked[0].is_reserved_type is True
    assert ranked[1].seat_type_label == "非對號列車"


def test_transfer_buffer_boundary_is_included() -> None:
    results = pair_transfers(
        [candidate("1", "08:00", "09:00", 60)],
        [candidate("2", "09:10", "10:00", 50)],
        transfer_station={"value": "1210-新竹", "label": "新竹"},
        direct_fastest_minutes=90,
    )
    assert len(results) == 1
    assert results[0]["buffer_minutes"] == 10
    assert "兩張車票" in results[0]["notice"]
    assert "不知道任一段是否有位" in results[0]["notice"]


def test_transfer_rejects_no_buffer_and_excess_duration() -> None:
    no_buffer = pair_transfers(
        [candidate("1", "08:00", "09:00", 60)],
        [candidate("2", "09:09", "10:00", 51)],
        transfer_station={"value": "1210-新竹", "label": "新竹"},
        direct_fastest_minutes=90,
    )
    too_slow = pair_transfers(
        [candidate("1", "08:00", "09:00", 60)],
        [candidate("2", "09:10", "11:00", 110)],
        transfer_station={"value": "1210-新竹", "label": "新竹"},
        direct_fastest_minutes=90,
    )
    assert no_buffer == []
    assert too_slow == []


def test_same_through_train_is_not_offered_as_a_transfer() -> None:
    """A train stopping at the hub appears in both OD queries; it is not a transfer."""
    hub = {"value": "3300-臺中", "label": "臺中"}
    # Train 109 reaches the hub at 09:30 and leaves it at 09:45: a long dwell,
    # which clears the 10 minute buffer and would otherwise pair with itself.
    first = candidate("109", "08:00", "09:30", 90)
    same_train = candidate("109", "09:45", "11:00", 75)
    other_train = candidate("155", "09:45", "11:00", 75)

    paired = pair_transfers(
        [first],
        [same_train, other_train],
        transfer_station=hub,
        direct_fastest_minutes=200,
    )

    assert [item["second_leg"]["train_no"] for item in paired] == ["155"]


def test_transfers_are_offered_when_no_direct_train_exists() -> None:
    """No direct service is exactly when a transfer is the only way to travel."""
    hub = {"value": "3300-臺中", "label": "臺中"}
    paired = pair_transfers(
        [candidate("109", "08:00", "10:00", 120)],
        [candidate("155", "10:20", "13:00", 160)],
        transfer_station=hub,
        direct_fastest_minutes=0,
    )

    assert len(paired) == 1
    assert paired[0]["duration_minutes"] == 300


def test_absolute_ceiling_still_applies_without_a_direct_baseline() -> None:
    hub = {"value": "3300-臺中", "label": "臺中"}
    paired = pair_transfers(
        [candidate("109", "06:00", "08:00", 120)],
        [candidate("155", "08:30", "20:00", 690)],
        transfer_station=hub,
        direct_fastest_minutes=0,
    )

    assert paired == [], "a 14 hour itinerary is not a usable suggestion"


def test_a_train_that_already_left_today_is_not_offered() -> None:
    # Booking one is impossible, and offering it sends the person into a round
    # that can only fail. The ±60 minute window slack makes this worse: asking
    # for 13:00 onwards at 16:02 would otherwise surface a 12:xx departure.
    records = [
        record("133", "自強", "1", "15:44", "16:42"),
        record("179", "自強", "1", "17:53", "18:52"),
    ]
    remaining = candidates_from_records(records, "13:00", "23:30", not_before=16 * 60 + 2)
    assert [item.train_no for item in remaining] == ["179"]


def test_minutes_already_past_only_applies_to_the_ride_day() -> None:
    now = datetime(2026, 9, 6, 16, 2, tzinfo=TAIPEI)
    assert minutes_already_past("2026/09/06", now) == 16 * 60 + 2
    assert minutes_already_past("2026/09/07", now) is None
    # The server runs on UTC; the timetable does not.
    assert minutes_already_past("2026/09/06", datetime(2026, 9, 6, 8, 2, tzinfo=UTC)) == 962
