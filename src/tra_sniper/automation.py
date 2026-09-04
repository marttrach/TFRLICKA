from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def choose_station_suggestion(query: str, suggestions: list[str]) -> int:
    """Return the best autocomplete result index without guessing a different station."""
    normalized = query.strip()
    if not normalized:
        raise ValueError("Station query cannot be empty")

    for index, suggestion in enumerate(suggestions):
        if suggestion.strip() == normalized:
            return index

    exact_name = [
        index
        for index, suggestion in enumerate(suggestions)
        if suggestion.partition("-")[2].strip() == normalized
    ]
    if len(exact_name) == 1:
        return exact_name[0]
    if len(exact_name) > 1:
        raise ValueError(
            f"Station {normalized!r} is ambiguous; use the full code-name value: "
            + ", ".join(suggestions[index] for index in exact_name)
        )

    raise ValueError(
        f"Station {normalized!r} was not found in suggestions: {', '.join(suggestions)}"
    )


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
                browser = playwright.chromium.connect_over_cdp(self.cdp_url)
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
        self._accept_cookie_notice(page)
        page.locator(
            f"input[name='custIdTypeEnum'][value='{request.identity_type.value}']"
        ).check()
        page.locator("#pid").fill(request.identity)

        self._select_station(page, "#startStation", request.start_station)
        self._select_station(page, "#endStation", request.end_station)

        page.locator(f"input[name='tripType'][value='{request.trip_type.value}']").check()
        page.locator(f"input[name='orderType'][value='{request.order_type.value}']").check()
        page.locator("#normalQty").fill(str(request.quantity))

        self._fill_leg(page, request.outbound, leg_index=0, order_type=request.order_type)
        if request.trip_type is TripType.ROUNDTRIP and request.inbound:
            self._fill_leg(page, request.inbound, leg_index=1, order_type=request.order_type)

        page.locator(
            "input[name='ticketOrderParamList[0].seatPref']"
            f"[value='{request.seat_preference.value}']"
        ).check()
        page.locator("#chgSeat1").set_checked(request.allow_seat_change)
        if request.trip_type is TripType.ROUNDTRIP:
            page.locator(
                "input[name='ticketOrderParamList[1].seatPref']"
                f"[value='{request.seat_preference.value}']"
            ).check()
            page.locator("#chgSeat2").set_checked(request.allow_seat_change)

    @staticmethod
    def _accept_cookie_notice(page: Any) -> None:
        button = page.get_by_role("button", name="接受並關閉")
        if button.count() and button.first.is_visible():
            button.first.click()

    @staticmethod
    def _select_station(page: Any, selector: str, station: str) -> None:
        field = page.locator(selector)
        field.fill(station)
        menu = page.locator("ul.ui-autocomplete:visible").last
        menu.wait_for(state="visible", timeout=10_000)
        items = menu.locator("li")
        suggestions = [text.strip() for text in items.all_inner_texts() if text.strip()]
        index = choose_station_suggestion(station, suggestions)
        items.nth(index).click()
        selected = field.input_value().strip()
        if "-" not in selected:
            raise RuntimeError(f"TRC did not accept station selection {station!r}")

    @staticmethod
    def _fill_leg(page: Any, leg: Leg, *, leg_index: int, order_type: OrderType) -> None:
        suffix = leg_index + 1
        page.locator(f"#rideDate{suffix}").fill(leg.ride_date)
        radio_number = 1 + leg_index * 2 if leg.search_by_departure else 2 + leg_index * 2
        page.locator(f"#startOrEndTime{radio_number}").check()

        if order_type is OrderType.BY_TRAIN_NO:
            offset = leg_index * 3
            for item_index, train_number in enumerate(leg.train_numbers, start=1):
                page.locator(f"#trainNoList{offset + item_index}").fill(train_number)
        else:
            page.locator(f"#startTime{suffix}").select_option(label=leg.start_time)
            page.locator(f"#endTime{suffix}").select_option(label=leg.end_time)

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
