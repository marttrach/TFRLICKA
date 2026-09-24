import sys
from datetime import UTC, datetime
from types import ModuleType

import pytest

from tra_sniper import models
from tra_sniper.automation import (
    CAPTCHA_PROMPT,
    CAPTCHA_WRONG,
    AutomationResult,
    TRCBookingAutomator,
)
from tra_sniper.browser_session import BookingSessionManager, run_booking_session
from tra_sniper.models import Leg, OrderType


class PlaywrightError(Exception):
    pass


@pytest.fixture(autouse=True)
def fake_playwright(monkeypatch):
    # CI installs no browser extra; the wait loop only needs the Error class.
    sync_api = ModuleType("playwright.sync_api")
    sync_api.Error = PlaywrightError
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


class FakePage:
    """Plays back official page states, one per wait_for_timeout tick."""

    def __init__(self, states: list[tuple[str, bool]]) -> None:
        self.states = states
        self.index = -1
        self.url = "https://www.trc.com.tw/tra-tip-web/tip/tip001/tip121/query"

    def wait_for_timeout(self, milliseconds: int) -> None:
        del milliseconds
        self.index = min(self.index + 1, len(self.states) - 1)

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(self, selector)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page, self.selector = page, selector

    def inner_text(self, timeout: int) -> str:
        del timeout
        return self.page.states[self.page.index][0]

    def is_visible(self) -> bool:
        return self.selector == "#codeimg" and self.page.states[self.page.index][1]


def wait(page: FakePage, prompts: list[str]) -> AutomationResult:
    return TRCBookingAutomator._wait_for_human_verification(
        page, wait_seconds=60, screenshot_path=None, on_progress=prompts.append
    )


def test_captcha_prompts_are_reported_once_and_a_wrong_code_keeps_the_round() -> None:
    prompts: list[str] = []
    page = FakePage([
        ("訂票表單", False),
        ("因 v3 驗證未通過，請輸入驗證碼", True),
        ("因 v3 驗證未通過，請輸入驗證碼", True),
        ("驗證碼錯誤", True),
        ("驗證碼錯誤", True),
        ("訂票成功 訂票代碼：1234567", False),
    ])

    result = wait(page, prompts)

    assert result.status == "completed"
    assert result.booking_code == "1234567"
    assert prompts == [CAPTCHA_PROMPT, CAPTCHA_WRONG]


def test_booking_failure_still_ends_the_round() -> None:
    result = wait(FakePage([("訂票失敗：該車次已無座位", False)]), [])
    assert result.status == "failed"


def test_prompt_reaches_the_session_only_while_waiting_for_the_person() -> None:
    manager = BookingSessionManager()
    session = manager.acquire("task-1", 7)
    seen: list[str] = []

    class Automator:
        def run(self, request, **kwargs):
            kwargs["on_progress"]("ignored before hand-off")
            kwargs["on_ready"]()
            kwargs["on_progress"](CAPTCHA_PROMPT)
            seen.append(session.message)
            return AutomationResult(status="completed", url="", message="done", booking_code="1234567")

    run_booking_session(session, automator=Automator(), request=object(), on_finish=lambda s: None)

    assert seen == [CAPTCHA_PROMPT]
    assert session.status == "completed"


def test_past_ride_date_uses_taiwan_calendar(monkeypatch) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            # 20:00 UTC is already the next day in Taiwan.
            return datetime(2026, 9, 24, 20, 0, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(models, "datetime", Clock)
    with pytest.raises(ValueError, match="in the past"):
        Leg("2026/09/24", ("110",)).validate(OrderType.BY_TRAIN_NO)
    Leg("2026/09/25", ("110",)).validate(OrderType.BY_TRAIN_NO)
