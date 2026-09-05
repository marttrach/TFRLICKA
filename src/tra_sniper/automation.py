from __future__ import annotations

import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import BookingRequest, Leg, OrderType, TripType
from .verification import VerificationMode, VerificationProvider, create_verification_provider

BOOKING_URL = "https://www.trc.com.tw/tra-tip-web/tip/tip001/tip121/query"
MEMBER_LOGIN_URL = "https://www.trc.com.tw/tra-tip-web/tip/tip008/tip811/memberLogin"


@dataclass(frozen=True, slots=True)
class AutomationResult:
    status: str
    url: str
    message: str
    booking_code: str | None = None
    screenshot: str | None = None


def station_code(station: str) -> str:
    """Return the numeric key the official <select> uses for a station.

    Stations are stored as "1180-竹北"; the option value is just "1180".
    "1001-臺北-環島" is a real entry, so only the leading segment is the code.
    """
    code = station.split("-", 1)[0].strip()
    if not code.isdigit():
        raise ValueError(f"車站 {station!r} 沒有可用的站碼")
    return code


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
    try:
        ip = socket.getaddrinfo(
            parts.hostname, parts.port, socket.AF_INET, socket.SOCK_STREAM
        )[0][4][0]
    except OSError:
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

    def run(
        self,
        request: BookingRequest,
        *,
        submit: bool = False,
        wait_seconds: int = 600,
        screenshot: str | Path | None = None,
        stop_event: threading.Event | None = None,
    ) -> AutomationResult:
        request.validate()
        if submit and self.headless:
            raise ValueError("Submission requires a headed browser for manual reCAPTCHA")
        if request.member_login and self.headless:
            raise ValueError("Member login requires a headed browser for official verification")

        from playwright.sync_api import sync_playwright

        screenshot_path = Path(screenshot).resolve() if screenshot else None
        if screenshot_path:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            if self.cdp_url:
                browser = playwright.chromium.connect_over_cdp(cdp_url_over_ipv4(self.cdp_url))
            else:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    slow_mo=self.slow_mo_ms,
                )
            # A fresh context per session is a security requirement, not a
            # preference: the sidecar browser outlives this booking, so a shared
            # context would carry one member login into the next task.
            context = browser.new_context(locale="zh-TW")
            page = context.new_page()
            try:
                if request.member_login:
                    page.goto(MEMBER_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
                    self._prepare_member_login(page, request.member_login.account, request.member_login.password)
                    print(
                        "臺鐵會員帳號與密碼已填入。請在瀏覽器確認，若官方要求驗證，"
                        "請完成官方驗證後按登入；登入完成後再前往訂票頁。"
                    )
                    self._wait_for_member_login(
                        page, wait_seconds=wait_seconds, stop_event=stop_event
                    )
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
                return self._wait_for_human_verification(
                    page,
                    wait_seconds=wait_seconds,
                    screenshot_path=screenshot_path,
                    stop_event=stop_event,
                )
            finally:
                context.close()
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

    def _prepare_member_login(self, page: Any, account: str, password: str) -> None:
        self._accept_cookie_notice(page)
        page.locator("#username").fill(account)
        page.locator("#password").fill(password)

    @staticmethod
    def _wait_for_member_login(
        page: Any, *, wait_seconds: int, stop_event: threading.Event | None = None
    ) -> None:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("訂票 session 已取消；未嘗試略過官方驗證")
            if "/tip811/memberLogin" not in page.url:
                return
            page.wait_for_timeout(1_000)
        raise RuntimeError("等待台鐵會員登入逾時；未嘗試略過官方驗證")

    def _prepare_form(self, page: Any, request: BookingRequest) -> None:
        # The official site splits 依車次/依時段 and 單程/雙行程 across four
        # separate URLs, and orderType/tripType are hidden inputs set by
        # whichever page you land on. BOOKING_URL is 依車次單程, so anything
        # else would silently fill the wrong form.
        if request.trip_type is not TripType.ONEWAY or request.order_type is not OrderType.BY_TRAIN_NO:
            raise NotImplementedError(
                "只支援依車次單程訂票；官方把其他組合放在不同網址上"
            )

        self._accept_cookie_notice(page)
        page.locator(
            f"input[name='custIdTypeEnum'][value='{request.identity_type.value}']"
        ).check()
        page.locator("#pid").fill(request.identity)

        page.locator("#startStation0").select_option(value=station_code(request.start_station))
        page.locator("#endStation0").select_option(value=station_code(request.end_station))
        page.locator("#normalQty0").select_option(value=str(request.quantity))

        self._fill_leg(page, request.outbound)

        page.locator("#seatPref0").select_option(value=request.seat_preference.value)
        page.locator("#chgSeat0").select_option(
            value="true" if request.allow_seat_change else "false"
        )

    @staticmethod
    def _accept_cookie_notice(page: Any) -> None:
        button = page.get_by_role("button", name="接受並關閉")
        if button.count() and button.first.is_visible():
            button.first.click()

    @staticmethod
    def _fill_leg(page: Any, leg: Leg) -> None:
        # rideDate is a <select> of roughly the next 30 days. Reading the
        # options first turns "date is past the booking window" into a message
        # that says so, instead of Playwright's opaque strict-mode failure.
        available = page.locator("#rideDate0 option").all_attribute_values("value")
        if leg.ride_date not in available:
            window = f"{available[0]} 至 {available[-1]}" if available else "（頁面未提供日期選項）"
            raise ValueError(
                f"官方訂票頁沒有 {leg.ride_date} 這個日期；目前開放 {window}"
            )
        page.locator("#rideDate0").select_option(value=leg.ride_date)

        # The first field's id is trainNo1 while its name is trainNoList[0].
        # Addressing all three by name keeps one code path off that quirk.
        for index, train_number in enumerate(leg.train_numbers):
            page.locator(
                f"input[name='ticketOrderParamList[0].trainNoList[{index}]']"
            ).fill(train_number)

    @staticmethod
    def _wait_for_human_verification(
        page: Any,
        *,
        wait_seconds: int,
        screenshot_path: Path | None,
        stop_event: threading.Event | None = None,
    ) -> AutomationResult:
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
            body_text = page.locator("body").inner_text(timeout=5_000)
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
