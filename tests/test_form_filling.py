"""The official booking form contract, as read off tip121/query on 2026-09-06.

Every field is a text input, radio or checkbox -- the earlier "#startStation0
<select>" contract matched nothing on the page and timed out on every round.
These assert the selectors and values we send, not a live page: CI installs no
browser, and the point is to catch a silent drift from that contract.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock, Mock

import pytest

from tra_sniper.automation import LEG0, TRCBookingAutomator
from tra_sniper.models import BookingRequest


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def _record(self, action, value):
        self.page.calls.append((self.selector, action, value))

    def fill(self, value):
        self.page.values[self.selector] = value
        self._record("fill", value)

    def check(self):
        self._record("check", None)

    def set_checked(self, checked):
        self._record("set_checked", checked)

    def blur(self):
        self._record("blur", None)

    def input_value(self):
        return self.page.values.get(self.selector, "")

    def count(self):
        return 0


class FakePage:
    """Records what would be done to the page, so CI needs no browser."""

    def __init__(self):
        self.calls = []
        self.values = {}

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_role(self, role, name=None):
        return FakeLocator(self, f"role:{role}:{name}")


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


def test_form_drives_the_official_controls():
    calls = prepared()
    # Stations are jQuery-UI autocompletes holding the whole "code-name" tag.
    assert ("#startStation", "fill", "1180-竹北") in calls
    assert ("#endStation", "fill", "2200-大甲") in calls
    assert ("#pid", "fill", "A123456789") in calls
    assert ("#normalQty", "fill", "2") in calls
    assert (f"input[name='{LEG0}.rideDate']", "fill", "2026/09/25") in calls
    assert (f"input[name='{LEG0}.seatPref'][value='NONE']", "check", None) in calls
    assert (f"input[name='{LEG0}.chgSeat']", "set_checked", True) in calls


def test_a_station_the_autocomplete_rejects_is_an_error_not_a_wrong_route():
    # The official script empties the field when nothing matches its tag list.
    page = FakePage()
    page.locator = lambda selector: _Emptying(page, selector)
    with pytest.raises(ValueError, match="不認得車站"):
        prepared(page)


class _Emptying(FakeLocator):
    def fill(self, value):
        super().fill(value)
        if "Station" in self.selector:
            self.page.values[self.selector] = ""


def test_form_uses_real_playwright_locator_methods():
    # The browser extra is optional; checking its API needs no browser process.
    playwright = pytest.importorskip("playwright.sync_api")
    page = FakePage()
    real = page.locator
    page.locator = Mock(side_effect=lambda selector: Mock(
        spec_set=playwright.Locator, wraps=real(selector),
    ))
    # spec_set rejects any attribute Locator does not really have, so getting
    # through _prepare_form proves fill/check/set_checked/blur/input_value are
    # all real methods -- the exact class of bug the old contract shipped.
    prepared(page)


def test_order_type_and_trip_type_are_set_rather_than_assumed():
    # Radio pairs on this same page. They happen to default to 依車次單程
    # today, which is exactly why leaving them alone would fail silently.
    calls = prepared()
    assert ("input[name='tripType'][value='ONEWAY']", "check", None) in calls
    assert ("input[name='orderType'][value='BY_TRAIN_NO']", "check", None) in calls


def test_train_numbers_go_to_their_own_zero_based_fields():
    calls = prepared(outbound={"ride_date": "2026/09/25", "train_numbers": ["123", "456", "789"]})
    for index, number in enumerate(["123", "456", "789"]):
        selector = f"input[name='ticketOrderParamList[0].trainNoList[{index}]']"
        assert (selector, "fill", number) in calls


def test_seat_change_false_unchecks_the_box():
    assert (f"input[name='{LEG0}.chgSeat']", "set_checked", False) in prepared(
        allow_seat_change=False)


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


def _fake_playwright(monkeypatch):
    playwright = MagicMock()
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = MagicMock()
    sync_api.sync_playwright.return_value.__enter__.return_value = playwright
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return playwright


def test_sidecar_booking_reuses_the_window_the_person_is_watching(monkeypatch):
    # A second context is a second top-level window, and the sidecar runs no
    # window manager: that window takes no keyboard input over VNC.
    page = FakePage()
    page.goto = Mock()
    automator = TRCBookingAutomator(cdp_url="http://browser:9222")
    page.url = automator.booking_url
    playwright = _fake_playwright(monkeypatch)
    browser = playwright.chromium.connect_over_cdp.return_value
    context = MagicMock()
    context.pages = [page]
    browser.contexts = [context]

    assert automator.run(booking()).status == "prepared"
    browser.new_context.assert_not_called()
    context.new_page.assert_not_called()
    # Cleared going in, and the desktop left blank for whoever looks next.
    assert context.clear_cookies.call_count == 2
    assert page.goto.call_args.args[0] == "about:blank"


def test_legacy_member_credentials_never_open_a_login_page(monkeypatch):
    # No browser dependency in CI: drive the real run() with a recording page.
    page = FakePage()
    page.goto = Mock()
    automator = TRCBookingAutomator(headless=True)
    automator.cdp_url = None
    page.url = automator.booking_url
    playwright = _fake_playwright(monkeypatch)
    browser = playwright.chromium.launch.return_value
    context = browser.new_context.return_value
    context.new_page.return_value = page

    result = automator.run(booking(member_login={"account": "saved", "password": "secret"}))
    assert result.status == "prepared"
    page.goto.assert_called_once_with(automator.booking_url, wait_until="domcontentloaded", timeout=60_000)
    assert not any(selector in {"#username", "#password"} for selector, _, _ in page.calls)
    context.close.assert_called_once()
