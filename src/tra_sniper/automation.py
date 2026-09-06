from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

from .models import BookingRequest, Leg, OrderType, TripType
from .verification import VerificationMode, VerificationProvider, create_verification_provider

BOOKING_URL = "https://www.trc.com.tw/tra-tip-web/tip/tip001/tip121/query"
# Every per-leg field is named for its trip index; only the outbound leg is used.
LEG0 = "ticketOrderParamList[0]"


@dataclass(frozen=True, slots=True)
class AutomationResult:
    status: str
    url: str
    message: str
    booking_code: str | None = None
    screenshot: str | None = None


def ipv4_host(host: str) -> str:
    """Resolve a container name to its A record, or return it unchanged.

    Docker's embedded DNS puts the AAAA record first on an IPv6-enabled
    network, and neither the socat relay nor x11vnc in docker/start-browser.sh
    listens on IPv6. Connecting by name therefore burns a doomed attempt first.
    """
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except OSError:
        return host


def cdp_url_over_ipv4(url: str) -> str:
    """Swap a CDP URL's hostname for its A record.

    Chromium rejects a DevTools request whose Host header is not an IP address
    or localhost, so `http://tra-sniper-browser:9222` answers 500 no matter how
    reachable the container is. It also builds the returned
    webSocketDebuggerUrl from that same header, so the address we ask with is
    the address Playwright then dials back on.

    IPv4 specifically, for two reasons: Docker's embedded DNS puts the AAAA
    record first on an IPv6-enabled network, and the socat relay in
    docker/start-browser.sh listens on IPv4 only.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        return url
    ip = ipv4_host(parts.hostname)
    if ip == parts.hostname:
        return url
    return parts._replace(netloc=f"{ip}:{parts.port}" if parts.port else ip).geturl()


class TRCBookingAutomator:
    def __init__(
        self,
        *,
        headless: bool = False,
        slow_mo_ms: int = 0,
        booking_url: str = BOOKING_URL,
        verification_provider: VerificationProvider | None = None,
        cdp_url: str | None = None,
    ) -> None:
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.booking_url = booking_url
        self.verification = verification_provider or create_verification_provider()
        # Unset means "launch a browser here", which keeps the CLI working on a
        # laptop with no sidecar container in sight.
        self.cdp_url = cdp_url or os.getenv("TRA_BROWSER_CDP_URL") or None

    def reset_browser(self) -> None:
        """Recover the dedicated sidecar, not just its abandoned Python worker."""
        if not self.cdp_url:
            raise RuntimeError("無法安全重啟本機瀏覽器；請重啟 API 後再試")
        from playwright.async_api import async_playwright

        def browser_id() -> str:
            endpoint = urlsplit(cdp_url_over_ipv4(self.cdp_url))._replace(
                scheme="http", path="/json/version", query="", fragment="",
            ).geturl()
            with urlopen(endpoint, timeout=1) as response:
                return json.load(response)["webSocketDebuggerUrl"]

        async def reset() -> None:
            async with asyncio.timeout(20), async_playwright() as playwright:
                original_id = await asyncio.to_thread(browser_id)
                url = cdp_url_over_ipv4(self.cdp_url)
                browser = await playwright.chromium.connect_over_cdp(url, timeout=5_000)
                cdp = await browser.new_browser_cdp_session()
                await cdp.send("Browser.close")
                while browser.is_connected():
                    await asyncio.sleep(0.1)
                # start-browser.sh exits when Chromium exits; the sidecar's
                # restart policy brings back a clean desktop. Wait for it so
                # the next task cannot race the container startup.
                while True:
                    try:
                        if await asyncio.to_thread(browser_id) != original_id:
                            return
                    except OSError:
                        pass
                    await asyncio.sleep(0.5)

        asyncio.run(reset())

    def run(
        self,
        request: BookingRequest,
        *,
        submit: bool = False,
        wait_seconds: int = 600,
        screenshot: str | Path | None = None,
        stop_event: threading.Event | None = None,
        on_ready: Callable[[], None] | None = None,
    ) -> AutomationResult:
        request.validate()
        if submit and self.headless:
            raise ValueError("Submission requires a headed browser for manual reCAPTCHA")

        from playwright.sync_api import sync_playwright

        screenshot_path = Path(screenshot).resolve() if screenshot else None
        if screenshot_path:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + wait_seconds

        with sync_playwright() as playwright:
            if self.cdp_url:
                browser = playwright.chromium.connect_over_cdp(cdp_url_over_ipv4(self.cdp_url))
                # Drive the window the sidecar already opened. A fresh context
                # opens a SECOND top-level window, and the sidecar's Xvfb has no
                # window manager to size, stack or focus it: the person watching
                # over VNC gets a form that ignores their keyboard. Clearing
                # cookies is what the fresh context was really buying, and the
                # browser outliving the booking is exactly why it is needed.
                context = browser.contexts[0]
                context.clear_cookies()
                page = context.pages[0] if context.pages else context.new_page()
                owns_browser = False
            else:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    slow_mo=self.slow_mo_ms,
                )
                context = browser.new_context(locale="zh-TW")
                page = context.new_page()
                owns_browser = True
            try:
                # Booking starts directly, including legacy requests carrying
                # member_login. Logging in first adds an unnecessary challenge.
                page.goto(self.booking_url, wait_until="domcontentloaded", timeout=60_000)
                self._prepare_form(page, request)

                if screenshot_path:
                    page.screenshot(path=str(screenshot_path), full_page=True)

                if not submit:
                    return AutomationResult(
                        status="prepared",
                        url=page.url,
                        message="Form prepared; no booking request was submitted.",
                        screenshot=str(screenshot_path) if screenshot_path else None,
                    )

                print(
                    "The form is ready. Complete the official CAPTCHA/reCAPTCHA in the "
                    "browser, review the details, and click 訂票 yourself."
                )
                if self.verification.capabilities.mode is not VerificationMode.MANUAL:
                    self._prepare_provider_handoff(page)
                if on_ready:
                    on_ready()
                return self._wait_for_human_verification(
                    page,
                    wait_seconds=max(0, int(deadline - time.monotonic())),
                    screenshot_path=screenshot_path,
                    stop_event=stop_event,
                )
            finally:
                if owns_browser:
                    context.close()
                else:
                    # Leave the shared desktop blank and cookie-free: the VNC
                    # stream outlives this call by a moment, and the next round
                    # must not inherit this traveller's session.
                    with suppress(Exception):
                        page.goto("about:blank", timeout=10_000)
                    with suppress(Exception):
                        context.clear_cookies()
                # For a CDP connection this only disconnects; the sidecar keeps
                # running, which is the whole point of it being a sidecar.
                browser.close()

    def _prepare_provider_handoff(self, page: Any) -> None:
        """Exercise the provider contract without submitting the booking form."""
        token = self.verification.authorize(target_url=page.url)
        if token is None:
            return
        field = page.locator("[data-tra-verification-token]")
        if field.count() != 1:
            raise RuntimeError(
                "verification provider hand-off field was not found; "
                "the adapter and target contract do not match"
            )
        field.fill(token)

    def _prepare_form(self, page: Any, request: BookingRequest) -> None:
        # 依車次/依時段 and 單程/來回 are radio pairs on this one page, not four
        # separate URLs. Only 依車次單程 is implemented, and the radios are set
        # explicitly rather than trusted to still default the way they do today.
        if request.trip_type is not TripType.ONEWAY or request.order_type is not OrderType.BY_TRAIN_NO:
            raise NotImplementedError("只支援依車次單程訂票")

        self._accept_cookie_notice(page)
        page.locator(
            f"input[name='custIdTypeEnum'][value='{request.identity_type.value}']"
        ).check()
        page.locator("#pid").fill(request.identity)

        page.locator("input[name='tripType'][value='ONEWAY']").check()
        page.locator("input[name='orderType'][value='BY_TRAIN_NO']").check()

        self._fill_station(page, "#startStation", request.start_station)
        self._fill_station(page, "#endStation", request.end_station)
        # A text input with -/+ buttons, not a <select>.
        page.locator("#normalQty").fill(str(request.quantity))

        self._fill_leg(page, request.outbound)

        page.locator(
            f"input[name='{LEG0}.seatPref'][value='{request.seat_preference.value}']"
        ).check()
        page.locator(f"input[name='{LEG0}.chgSeat']").set_checked(request.allow_seat_change)

    @staticmethod
    def _accept_cookie_notice(page: Any) -> None:
        button = page.get_by_role("button", name="接受並關閉")
        if button.count() and button.first.is_visible():
            button.first.click()

    @staticmethod
    def _fill_station(page: Any, selector: str, station: str) -> None:
        """Fill a jQuery-UI autocomplete whose value must be one of its own tags.

        The field is plain text carrying "1180-竹北", and on blur the official
        script rewrites whatever is there to the matching tag -- or empties it
        when nothing matches. Filling the stored label lands on the tag exactly;
        blurring afterwards lets the site normalise and, if it wipes the field,
        turns a silently empty station into an error here instead of a booking
        for the wrong route.
        """
        field = page.locator(selector)
        field.fill(station)
        field.blur()
        if not field.input_value().strip():
            raise ValueError(f"官方訂票頁不認得車站 {station!r}；站名或站碼可能已變更")

    @staticmethod
    def _fill_leg(page: Any, leg: Leg) -> None:
        # A datepicker text input taking YYYY/MM/DD, validated as dateISO. Out
        # of range is the official page's call to make, not ours.
        page.locator(f"input[name='{LEG0}.rideDate']").fill(leg.ride_date)

        # The first field's id is trainNoList1 while its name is trainNoList[0].
        # Addressing all three by name keeps one code path off that quirk.
        for index, train_number in enumerate(leg.train_numbers):
            page.locator(f"input[name='{LEG0}.trainNoList[{index}]']").fill(train_number)

    @staticmethod
    def _wait_for_human_verification(
        page: Any,
        *,
        wait_seconds: int,
        screenshot_path: Path | None,
        stop_event: threading.Event | None = None,
    ) -> AutomationResult:
        from playwright.sync_api import Error as PlaywrightError

        deadline = time.monotonic() + wait_seconds
        last_url = page.url
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return AutomationResult(
                    status="cancelled",
                    url=last_url,
                    message="Booking session was cancelled before TRC returned a result.",
                )
            page.wait_for_timeout(1_000)
            last_url = page.url
            try:
                body_text = page.locator("body").inner_text(timeout=5_000)
            except PlaywrightError:
                # The person just pressed 訂票 and the page is navigating, so
                # this read lost its execution context. Failing the round here
                # would abandon a booking that is actually in flight; the next
                # tick reads the result page instead.
                continue
            code_match = re.search(
                r"(?:訂票|電腦|取票)(?:代碼|編號)\s*[:：]?\s*([A-Z0-9-]{6,})",
                body_text,
            )
            if code_match:
                if screenshot_path:
                    page.screenshot(path=str(screenshot_path), full_page=True)
                return AutomationResult(
                    status="completed",
                    url=last_url,
                    message="TRC returned a booking code.",
                    booking_code=code_match.group(1),
                    screenshot=str(screenshot_path) if screenshot_path else None,
                )
            if "驗證碼錯誤" in body_text or "訂票失敗" in body_text:
                return AutomationResult(
                    status="failed",
                    url=last_url,
                    message="TRC reported that verification or booking failed.",
                )

        return AutomationResult(
            status="timeout",
            url=last_url,
            message=(
                "Timed out while waiting for manual CAPTCHA/reCAPTCHA and the official TRC "
                "result. "
                "No CAPTCHA bypass was attempted."
            ),
            screenshot=str(screenshot_path) if screenshot_path else None,
        )
