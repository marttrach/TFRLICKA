"""Auto-submit booking: break reCAPTCHA v3 the honest way.

A v3 challenge has no image to solve; Google watches the browser and issues a
score. So "breaking" it here means running the official page inside a real
headless Chromium — genuine fingerprint, human-like input timing — and letting
the page's own ``grecaptcha.execute()`` mint a token with a usable score. No
token is forged, none is replayed.

Playwright is an optional extra (``pip install 'tra-sniper[autobook]'`` plus
``playwright install chromium``). It is imported lazily so the rest of the
package never requires it.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Booking-outcome markers, kept as one place to patch when TRA restyles the
# result page. Success must be recognised before failure: the confirmation
# page also contains form leftovers that read like errors.
SUCCESS_MARKERS = ("订票成功", "訂票成功", "预约成功", "預約成功", "预订成功", "預訂成功")
FAILURE_MARKERS = (
    "订票失败",
    "訂票失敗",
    "预约失败",
    "預約失敗",
    "已售完",
    "售完",
    "已满",
    "已滿",
    "订位失败",
    "訂位失敗",
    "验证失败",
    "驗證失敗",
)
# TRA's 電腦代碼: letters plus digits, optionally hyphen-grouped.
BOOKING_CODE_PATTERN = re.compile(r"[A-Z]{1,2}-?\d{5,9}|[A-Z]-?\d{4,}-?\d*")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Selectors for the official pre-filled page, tried in order. One place to fix
# when TRC ships a new booking UI.
IDENTITY_INPUT_SELECTORS = (
    "input#txtId",
    "input[name='id']",
    "input[placeholder*='身分證']",
    "input[placeholder*='身份']",
)
SUBMIT_BUTTON_SELECTORS = (
    "button:has-text('確認訂票')",
    "button:has-text('確定訂票')",
    "button:has-text('訂票')",
    "input[type=submit]",
)

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-TW', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

# ---- image-captcha fallback (live-verified 2026-11-03, see skill
# tra-online-booking): reCAPTCHA Enterprise v3 scores a datacenter IP 0 and the
# official page then shows a picture code. TRA accepts it case-insensitively.
CAPTCHA_MARKER = "驗證碼"
CAPTCHA_IMG_SELECTORS = (
    "img[src*='player/picture']",
    "#capImg",
    "img.captcha",
)
CAPTCHA_INPUT_SELECTORS = (
    "#verifyCode",
    "input[name='g-recaptcha-response']",
)
CAPTCHA_TYPE_FIELD = "input[name='verifyType']"
CAPTCHA_FALLBACK_IMAGE_PATH = "/tra-tip-web/tip/player/picture"


def page_shows_captcha(page_text: str) -> bool:
    return CAPTCHA_MARKER in (page_text or "")


class OcrChain:
    """Ranked subprocess OCRs: tries each until one prints a usable code.

    Defaults follow this host's verified pipeline (keras-ocr-pipeline.md):
    a self-trained CRNN first, ddddocr as fallback. Subprocess isolation keeps
    TensorFlow out of the API process entirely.
    """

    def __init__(self, commands, runner=None, timeout_s: float = 60.0) -> None:
        # commands: sequence of (name, argv_prefix); the image path is appended.
        self.commands = list(commands)
        self._run = runner or self._subprocess_run
        self.timeout_s = timeout_s
        self.last_used = ""

    @staticmethod
    def _subprocess_run(argv, timeout):
        import subprocess

        return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)

    def solve(self, image_path: str) -> str | None:
        for name, argv in self.commands:
            try:
                done = self._run([*argv, image_path], self.timeout_s)
            except Exception:  # noqa: BLE001, S112 - a broken OCR entry must not stop the chain
                continue
            if done.returncode != 0:
                continue
            # The keras predictor prints per-image lines; take the last one.
            lines = [line.strip() for line in done.stdout.strip().splitlines() if line.strip()]
            if not lines:
                continue
            code = lines[-1]
            if len(code) >= 4:
                self.last_used = name
                # TRA verification is case-insensitive (live A/B proven).
                return code.upper()
        return None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Outcome of one auto-submit attempt. ``reason`` explains any failure."""

    ok: bool
    booking_code: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "booking_code": self.booking_code, "reason": self.reason}


def parse_booking_outcome(page_text: str) -> SubmitResult:
    """Read a booking-result page's text without a browser.

    Pure function on purpose: the scheduler's behaviour is testable even where
    Playwright is not installed.
    """
    text = (page_text or "").strip()
    if not text:
        return SubmitResult(False, reason="頁面沒有內容")
    if any(marker in text for marker in SUCCESS_MARKERS):
        match = BOOKING_CODE_PATTERN.search(text.upper())
        return SubmitResult(True, booking_code=match.group(0) if match else None)
    for marker in FAILURE_MARKERS:
        if marker in text:
            return SubmitResult(False, reason=f"官方頁面回報：{marker}")
    return SubmitResult(False, reason="讀不到訂票結果（頁面可能已改版）")


class PlaywrightAutoSubmitter:
    """Drive one booking attempt end to end in a throwaway headless Chromium.

    Per-attempt browsers (not a warm pool) keep failures contained: a crashed
    or mutated page can never poison the next try. The cost is launch time,
    which is irrelevant next to a poll interval of minutes.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
        user_agent: str = DEFAULT_USER_AGENT,
        captcha_solver: OcrChain | None = None,
        captcha_retries: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.user_agent = user_agent
        # None disables the picture-code fallback entirely: a captcha page
        # then reads as "page not understood" and the task falls to reminders.
        self.captcha_solver = captcha_solver
        self.captcha_retries = captcha_retries
        self._sleep = sleeper
        self._rng = rng or random.Random()

    def _human_pause(self, low: float = 0.6, high: float = 1.8) -> None:
        self._sleep(self._rng.uniform(low, high))

    def submit(self, official_url: str, identity: str) -> SubmitResult:
        if not identity.strip():
            return SubmitResult(False, reason="任務沒有身分證資料，無法自動訂票")
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return SubmitResult(
                False,
                reason="自動訂票未安裝：pip install 'tra-sniper[autobook]' && playwright install chromium",
            )
        try:
            with sync_playwright() as playwright:
                return self._submit(playwright, official_url, identity.strip(), PlaywrightError)
        except PlaywrightError as exc:
            logger.warning(
                "auto-submit browser error",
                extra={"event": "autobook.browser_error"},
                exc_info=True,
            )
            return SubmitResult(False, reason=f"瀏覽器錯誤：{str(exc).splitlines()[0]}")

    def _submit(self, playwright: Any, official_url: str, identity: str, error_type: Any) -> SubmitResult:
        browser = playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                user_agent=self.user_agent,
                locale="zh-TW",
                timezone_id="Asia/Taipei",
                viewport={"width": 1280, "height": 900},
            )
            context.set_default_timeout(self.timeout_ms)
            context.add_init_script(STEALTH_SCRIPT)
            page = context.new_page()
            page.goto(official_url, wait_until="domcontentloaded")
            self._human_pause()

            identity_input = self._first_visible(page, IDENTITY_INPUT_SELECTORS)
            if identity_input is None:
                outcome = parse_booking_outcome(page.content())
                if outcome.ok or "官方頁面回報" in outcome.reason:
                    return outcome  # the page already resolved before we typed
                return SubmitResult(False, reason="找不到身分證欄位（頁面可能已改版）")

            identity_input.click()
            self._human_pause(0.2, 0.5)
            # Typing per character: reCAPTCHA v3 weighs keyboard cadence.
            identity_input.type(identity, delay=int(self._rng.uniform(60, 140)))
            self._human_pause()

            submit_button = self._first_visible(page, SUBMIT_BUTTON_SELECTORS)
            if submit_button is None:
                return SubmitResult(False, reason="找不到訂票按鈕（頁面可能已改版）")

            # The v3 token is minted on this interaction; wait for the result
            # page instead of a fixed sleep. A datacenter IP answers with the
            # picture-code fallback instead — read it, OCR it, try again.
            submit_button.click()
            deadline = self.timeout_ms / 1000.0
            waited = 0.0
            captcha_attempts = 0
            while waited < deadline:
                content = page.content()
                outcome = parse_booking_outcome(content)
                if outcome.ok or "官方頁面回報" in outcome.reason:
                    return outcome
                if (
                    self.captcha_solver is not None
                    and page_shows_captcha(content)
                    and captcha_attempts < self.captcha_retries
                ):
                    captcha_attempts += 1
                    if self._solve_captcha(page):
                        outcome = parse_booking_outcome(page.content())
                        if outcome.ok or "官方頁面回報" in outcome.reason:
                            return outcome
                        continue
                self._sleep(1.0)
                waited += 1.0
            outcome = parse_booking_outcome(page.content())
            if outcome.ok:
                return outcome
            if captcha_attempts and not page_shows_captcha(page.content()):
                raise error_type("圖形驗證碼連續失敗")
            raise error_type(outcome.reason or "等了很久仍讀不到訂票結果")
        finally:
            browser.close()

    def _solve_captcha(self, page: Any) -> bool:
        """Read the picture code, fill the form and resubmit. True on success.

        The image is fetched through the page's own request context so the
        session cookie (the picture is session-bound) comes along for free.
        """
        import os
        import tempfile
        from urllib.parse import urljoin

        assert self.captcha_solver is not None
        image = self._first_visible(page, CAPTCHA_IMG_SELECTORS)
        if image is None:
            return False
        src = image.get_attribute("src") or CAPTCHA_FALLBACK_IMAGE_PATH
        try:
            response = page.request.get(urljoin(page.url, f"{src.split('?')[0]}?r={os.getpid()}"))
            body = response.body()
        except Exception:  # noqa: BLE001 - unreadable picture means the round falls back
            return False
        code_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
                handle.write(body)
                code_path = handle.name
            code = self.captcha_solver.solve(code_path)
        finally:
            if code_path:
                with suppress(OSError):
                    os.unlink(code_path)
        if not code:
            return False

        field = self._first_visible(page, CAPTCHA_INPUT_SELECTORS)
        if field is None:
            return False
        field.fill(code)
        type_field = page.query_selector(CAPTCHA_TYPE_FIELD)
        if type_field is not None:
            type_field.evaluate("el => { el.value = 'text'; }")
        submit_button = self._first_visible(page, SUBMIT_BUTTON_SELECTORS)
        if submit_button is None:
            return False
        self._human_pause(0.2, 0.6)
        submit_button.click()
        return True

    @staticmethod
    def _first_visible(page: Any, selectors: tuple[str, ...]) -> Any | None:
        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError:  # unit tests run without playwright installed
            PlaywrightError = Exception

        for selector in selectors:
            try:
                handle = page.query_selector(selector)
                if handle is not None and handle.is_visible():
                    return handle
            except PlaywrightError:
                continue
        return None


def build_captcha_solver_from_env() -> OcrChain | None:
    """Keras CRNN first, ddddocr second; only entries installed on this host count."""
    import os
    from pathlib import Path

    root = Path(os.getenv("TRA_OCR_HOME", "/home/hermesbot/projects/tra_ocr"))
    commands = []
    keras_py = os.getenv("TRA_AUTOBOOK_KERAS_PY", str(root / "kerasvenv/bin/python"))
    keras_script = os.getenv("TRA_AUTOBOOK_KERAS_SCRIPT", str(root / "data/tra_predict.py"))
    if Path(keras_py).exists() and Path(keras_script).exists():
        commands.append(("keras", [keras_py, keras_script]))
    dddd_py = os.getenv("TRA_AUTOBOOK_DDDD_PY", str(root / "ocrvenv/bin/python"))
    if Path(dddd_py).exists():
        commands.append(("ddddocr", [dddd_py, "-c",
            "import sys,ddddocr; print(ddddocr.DdddOcr(show_ad=False).classification(open(sys.argv[1],'rb').read()))"]))
    return OcrChain(commands) if commands else None


def build_autobook_submitter_from_env() -> PlaywrightAutoSubmitter | None:
    """Return a submitter when TRA_AUTOBOOK_ENABLED is on; None means reminders only."""
    import os

    if os.getenv("TRA_AUTOBOOK_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        return None
    try:
        import playwright  # noqa: F401
    except ImportError:
        logger.warning(
            "TRA_AUTOBOOK_ENABLED is set but playwright is not installed; "
            "tasks fall back to reminders",
            extra={"event": "autobook.not_installed"},
        )
        return None
    headless = os.getenv("TRA_AUTOBOOK_HEADLESS", "1").strip().lower() not in {"0", "false", "no"}
    timeout_ms = int(os.getenv("TRA_AUTOBOOK_TIMEOUT_MS", "30000"))
    retries = int(os.getenv("TRA_AUTOBOOK_CAPTCHA_RETRIES", "3"))
    return PlaywrightAutoSubmitter(
        headless=headless,
        timeout_ms=timeout_ms,
        captcha_solver=build_captcha_solver_from_env(),
        captcha_retries=retries,
    )
