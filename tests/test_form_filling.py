"""The official booking form contract, as probed on 2026-09-05.

See docs/superpowers/specs/2026-09-05-booking-flow-rework-design.md. These
assert the selectors and values we send, not a live page: CI installs no
browser, and the point is to catch a silent drift from that contract.
"""

from unittest.mock import Mock

import pytest

from tra_sniper.automation import TRCBookingAutomator, station_code
from tra_sniper.models import BookingRequest

RIDE_DATES = ["2026/09/05", "2026/09/06", "2026/09/25", "2026/10/04"]


class FakeLocator:
    def __init__(self, calls, selector, ride_dates):
        self.calls = calls
        self.selector = selector
        self.ride_dates = ride_dates

    def _record(self, action, value):
        self.calls.append((self.selector, action, value))

    def fill(self, value):
        self._record("fill", value)

    def select_option(self, value=None, label=None):
        self._record("select_option", value if value is not None else label)

    def check(self):
        self._record("check", None)

    def evaluate_all(self, expression):
        assert expression == "options => options.map(option => option.value)"
        return list(self.ride_dates)

    def count(self):
        return 0


class FakePage:
    """Records what would be done to the page, so CI needs no browser."""

    def __init__(self, ride_dates=RIDE_DATES):
        self.calls = []
        self.ride_dates = ride_dates

    def locator(self, selector):
        return FakeLocator(self.calls, selector, self.ride_dates)

    def get_by_role(self, role, name=None):
        return FakeLocator(self.calls, f"role:{role}:{name}", self.ride_dates)


def booking(**overrides):
    data = {
        "identity": "A123456789",
        "start_station": "1180-竹北",
        "end_station": "2200-大甲",
        "quantity": 2,
        "order_type": "BY_TRAIN_NO",
        "outbound": {"ride_date": "2026/09/25", "train_numbers": ["123"]},
    }
    data.update(overrides)
    return BookingRequest.from_dict(data)


def prepared(page=None, **overrides):
    page = page or FakePage()
    TRCBookingAutomator()._prepare_form(page, booking(**overrides))
    return page.calls


def test_station_code_uses_the_official_numeric_key():
    assert station_code("1180-竹北") == "1180"
    # The official list has 1001-臺北-環島; only the leading code is the key.
    assert station_code("1001-臺北-環島") == "1001"


def test_station_without_a_numeric_code_is_rejected():
    with pytest.raises(ValueError, match="站碼"):
        station_code("竹北")


def test_form_drives_the_official_select_controls():
    calls = prepared()
    assert ("#startStation0", "select_option", "1180") in calls
    assert ("#endStation0", "select_option", "2200") in calls
    assert ("#rideDate0", "select_option", "2026/09/25") in calls
    assert ("#normalQty0", "select_option", "2") in calls
    assert ("#seatPref0", "select_option", "NONE") in calls
    assert ("#chgSeat0", "select_option", "true") in calls
    assert ("#pid", "fill", "A123456789") in calls


def test_form_uses_real_playwright_locator_methods():
    # The browser extra is optional; checking its API needs no browser process.
    playwright = pytest.importorskip("playwright.sync_api")
    page = FakePage()
    page.locator = Mock(side_effect=lambda selector: Mock(
        spec_set=playwright.Locator,
        wraps=FakeLocator(page.calls, selector, page.ride_dates),
    ))
    prepared(page)


def test_order_type_and_trip_type_are_never_clicked():
    # Both are hidden inputs now; the tab URL decides them.
    selectors = [selector for selector, _, _ in prepared()]
    assert not [s for s in selectors if "orderType" in s or "tripType" in s]


def test_train_numbers_go_to_their_own_zero_based_fields():
    calls = prepared(outbound={"ride_date": "2026/09/25", "train_numbers": ["123", "456", "789"]})
    for index, number in enumerate(["123", "456", "789"]):
        selector = f"input[name='ticketOrderParamList[0].trainNoList[{index}]']"
        assert (selector, "fill", number) in calls


def test_ride_date_outside_the_official_range_is_refused_clearly():
    page = FakePage(ride_dates=["2026/09/05", "2026/09/06"])
    with pytest.raises(ValueError) as excinfo:
        prepared(page)
    message = str(excinfo.value)
    assert "2026/09/25" in message
    assert "2026/09/06" in message


def test_seat_change_false_sends_the_string_false():
    assert ("#chgSeat0", "select_option", "false") in prepared(allow_seat_change=False)


def test_roundtrip_is_refused_rather_than_silently_wrong():
    with pytest.raises(NotImplementedError, match="依車次單程"):
        prepared(
            trip_type="ROUNDTRIP",
            inbound={"ride_date": "2026/09/26", "train_numbers": ["456"]},
        )


def test_by_time_is_refused_rather_than_silently_wrong():
    with pytest.raises(NotImplementedError, match="依車次單程"):
        prepared(
            order_type="BY_TIME",
            outbound={"ride_date": "2026/09/25", "start_time": "08:00", "end_time": "12:00"},
        )
