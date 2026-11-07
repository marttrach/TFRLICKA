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
    "已被停權",
    # Live 2026-11-07 (fake-id probe): the server answers a bad credential
    # with this AFTER the submit -- a clean identity failure, not unreadable.
    # Not in the initial HTML (grep-verified), so it cannot misfire pre-submit.
    "身分證字號格式錯誤",
)
# The FAILURE_MARKERS that mean "no seat right now", not "something broke".
SOLD_OUT_MARKERS = frozenset({"已售完", "售完", "已滿", "已满"})
# TRA's 電腦代碼. Live-verified 2026-11-07 (tip115 success page): the current
# code is PLAIN digits ("訂票代碼 2854451") — a letters-only pattern silently
# misses it. Label-anchored ONLY: unlabeled success text is a banner, not a
# booking (see parse_booking_outcome).
_BOOKING_CODE_LABELLED = re.compile(r"(?:訂票代碼|電腦訂票代碼|電腦代碼)[^0-9A-Z]{0,4}(\d{4,9})")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# tip123 wizard: query → queryTrain → bookingTicket(票種) → confirm → 完成.
# Six steps is the full walk with room for one re-render; a step's answer is
# awaited for at most STEP_DEADLINE_SECONDS before it counts as stuck.
WIZARD_STEPS = 6
STEP_DEADLINE_SECONDS = 20.0

# Selectors for the official pre-filled page, tried in order. One place to fix
# when TRC ships a new booking UI. The ``#pid``/``#queryForm`` pair is the
# live-verified shape of tip121/query (probed 2026-11-05): the identity field
# is ``input#pid`` with an empty placeholder, and the submit control is the
# only ``[type=submit]`` inside ``form#queryForm``. The older guesses stay as
# fallbacks, but ``:has-text`` is substring-matched — plain
# ``button:has-text('訂票')`` also hits the 個人訂票/團體訂票 tabs — so the
# specific selectors must stay first.
IDENTITY_INPUT_SELECTORS = (
    "input#pid",
    "input[name='pid']",
    "input#txtId",
    "input[name='id']",
    "input[placeholder*='身分證']",
    "input[placeholder*='身份']",
)
SUBMIT_BUTTON_SELECTORS = (
    "form#queryForm button[type=submit]",
    "form#queryForm input[type=submit]",
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

# Live-verified pitfall (run3.mjs, 2026-11-03): resubmitting the picture code
# on a page the server re-rendered arrives with these fields wiped — a blind
# resubmit only answers "輸入資料有誤" and burns every captcha retry. Capture
# the whole form before the first click and re-set it the instant before each
# resubmit, same as run3 does with setV.
FORM_STATE_SELECTORS = (
    "input#pid",
    "input[name='startStation']",
    "input[name='endStation']",
    'input[name="ticketOrderParamList[0].rideDate"]',
    'input[name="ticketOrderParamList[0].trainNoList[0]"]',
    'input[name="ticketOrderParamList[0].trainNoList[1]"]',
    'input[name="ticketOrderParamList[0].trainNoList[2]"]',
)
_JS_CAPTURE_FORM = """(selectors) => {
  const out = {values: {}, checked: ''};
  for (const s of selectors) { const el = document.querySelector(s); if (el) out.values[s] = el.value; }
  const r = document.querySelector('input[name=custIdTypeEnum]:checked');
  if (r) out.checked = r.value;
  return out;
}"""
_JS_RESTORE_FORM = """([values, checked]) => {
  for (const [s, v] of Object.entries(values)) { const el = document.querySelector(s); if (el) el.value = v; }
  if (checked) for (const r of document.querySelectorAll('input[name=custIdTypeEnum]')) r.checked = r.value === checked;
}"""

# tip123 (where the TDX deep link lands since 2026-11) is a wizard
# (query → queryTrain → bookingTicket → 完成) with every step gated by
# client-side jQuery Validate plus a reCAPTCHA Enterprise token that a
# headless browser can never mint. Live-verified 2026-11-06: the server
# itself happily accepts a plain form.submit() and runs its own checks
# (picture code, 停權, 售完), so every advance is a native submit.
_JS_ADVANCE_FORM = """() => {
  const forms = [...document.querySelectorAll('form')];
  const f = forms.find(x => x.querySelector('input[name^="selectLoc"]'))
        || document.querySelector('#queryForm')
        || forms.find(x => x.querySelector('input[type=submit],button[type=submit]'));
  if (!f) return false;
  f.submit();
  return true;
}"""
_JS_SELECT_TRAIN = """() => {
  const rows = [...document.querySelectorAll('input[name^="selectLoc"]')];
  if (!rows.length) return 0;
  // Live 2026-11-07: a no-train query lists many rows -- the page's own
  // pre-filled trainNo hidden input (179) must win over rows[0] or we book
  // the wrong train.
  const want = [...document.querySelectorAll('input[name*="trainNoList"]')]
    .map(i => (i.value || '').trim()).find(Boolean) || '';
  const target = (want && rows.find(r =>
      (r.closest('tr')?.innerText || '').includes(want)))
    || rows.find(r => r.checked) || rows[0];
  if (!target.checked) target.click();  // real click; the row 選擇 button would
  return 1;                              // re-fetch a fresh captcha (live pitfall)
}"""


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
        self.last_stderr = ""

    @staticmethod
    def _subprocess_run(argv, timeout):
        import subprocess

        return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)

    def solve(self, image_path: str) -> str | None:
        self.last_stderr = ""
        for name, argv in self.commands:
            try:
                done = self._run([*argv, image_path], self.timeout_s)
            except Exception:  # noqa: BLE001, S112 - a broken OCR entry must not stop the chain
                continue
            if done.returncode != 0:
                err = (getattr(done, "stderr", "") or "").strip().splitlines()
                self.last_stderr = err[-1] if err else ""
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
    """Outcome of one auto-submit attempt. ``reason`` explains any failure.

    ``unknown`` marks the one failure the scheduler must never re-queue: the
    訂票 click happened and the final page could not be read, so a second
    attempt might book a second real, paid ticket.
    """

    ok: bool
    booking_code: str | None = None
    reason: str = ""
    unknown: bool = False
    # No seat this round: the normal answer while waiting for one, so the
    # scheduler keeps polling instead of counting it as a failure.
    sold_out: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "booking_code": self.booking_code,
            "reason": self.reason,
            "unknown": self.unknown,
            "sold_out": self.sold_out,
        }


def parse_booking_outcome(page_text: str) -> SubmitResult:
    """Read a booking-result page's text without a browser.

    Pure function on purpose: the scheduler's behaviour is testable even where
    Playwright is not installed.
    """
    text = (page_text or "").strip()
    if not text:
        return SubmitResult(False, reason="頁面沒有內容")
    if any(marker in text for marker in SUCCESS_MARKERS):
        labeled = _BOOKING_CODE_LABELLED.search(text)
        if labeled:
            return SubmitResult(True, booking_code=labeled.group(1))
        # Success wording WITHOUT the labeled pure-digit code is not proof of
        # a booking: the TRA unpaid-order banner on landing/query pages also
        # says 已訂票成功 (live false positive 2026-11-07: the banner's order
        # number A15240000 was notified as this booking's code). Real tip115
        # pages always carry 訂票代碼 NNNNNNN (3/3 live) -- without one the
        # outcome is unknown; a human checks the official records.
        return SubmitResult(
            False, unknown=True, reason="讀到成功字樣但無電腦訂票代碼，請至訂票紀錄確認"
        )
    for marker in FAILURE_MARKERS:
        if marker in text:
            return SubmitResult(
                False, reason=f"官方頁面回報：{marker}", sold_out=marker in SOLD_OUT_MARKERS
            )
    return SubmitResult(False, reason="讀不到訂票結果（頁面可能已改版）")


def _resolve_deeplink(official_url: str) -> str:
    """Follow the TDX booking deeplink to its final trc.com.tw URL.

    The link hops through maas.transportdata.tw, which answers a datacenter
    IP with a Fortinet "Web Page Blocked" wall; the 302 itself is still
    served, so asking for Location and opening the final URL directly works
    (live-proven 2026-11-07 end-to-end booking 2854451).
    shortcut: one hop only; upgrade if TDX ever chains redirects.
    """
    if "deeplink/redirect" not in official_url:
        return official_url
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = urllib.request.Request(official_url, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=15) as response:
            response.read(0)
            return official_url  # answered 200: the page itself is usable
    except urllib.error.HTTPError as exc:
        target = exc.headers.get("Location") if exc.code in (301, 302, 303, 307, 308) else None
        from urllib.parse import urljoin

        return urljoin(official_url, target) if target else official_url
    except Exception:  # noqa: BLE001 - unresolvable link still deserves a browser try
        return official_url


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
        official_url = _resolve_deeplink(official_url)
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

    def _submit(
        self, playwright: Any, official_url: str, identity: str, error_type: Any
    ) -> SubmitResult:
        browser = playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                # Docker reality: the container runs Chromium as root and ships
                # a small /dev/shm; without these two the browser crashes on
                # launch or mid-page-load instead of failing gracefully.
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
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
            # The cookie banner covers the form on first visit (live pitfall).
            with suppress(Exception):
                page.evaluate(
                    """() => {
                        const b = [...document.querySelectorAll('button,a')]
                          .find(e => (e.textContent || '').trim() === '接受並關閉');
                        if (b) b.click();
                    }"""
                )
            self._human_pause()

            def read_content() -> str:
                """page.content() that survives navigation races.

                A just-accepted submit can leave Playwright raising
                'Unable to retrieve content' for a beat (the 2026-11-07
                tip115 miss: the booking had ALREADY succeeded, the read
                raced the navigation and the round parked as unknown).
                Poll until readable; the final raise keeps the
                post-submit unknown-park invariant intact.
                """
                last: Exception | None = None
                waited = 0.0
                while waited < STEP_DEADLINE_SECONDS:
                    try:
                        return page.content()
                    except error_type as exc:
                        last = exc
                        self._sleep(0.5)
                        waited += 0.5
                assert last is not None
                raise last

            identity_input = self._first_visible(page, IDENTITY_INPUT_SELECTORS)
            if identity_input is None:
                outcome = parse_booking_outcome(read_content())
                if outcome.ok or "官方頁面回報" in outcome.reason:
                    return outcome  # the page already resolved before we typed
                # Name the page we actually landed on: the last incident was a
                # selector miss, and "頁面可能已改版" alone could not tell a
                # wrong page from a redesigned one.
                where = page.url.split("?", 1)[0].rsplit("//", 1)[-1][:60]
                return SubmitResult(False, reason=f"找不到身分證欄位（頁面可能已改版：{where}）")

            identity_input.click()
            self._human_pause(0.2, 0.5)
            # Typing per character: reCAPTCHA v3 weighs keyboard cadence.
            identity_input.type(identity, delay=int(self._rng.uniform(60, 140)))
            self._human_pause()

            # Capture the full form (station/date/train/pid) the moment before
            # the first submit; captcha resubmits must restore it verbatim.
            try:
                prefill = page.evaluate(_JS_CAPTURE_FORM, list(FORM_STATE_SELECTORS))
            except Exception:  # noqa: BLE001 - capture is best-effort recovery aid
                prefill = {"values": {}, "checked": ""}

            def settle(prev_url: str, prev_content: str) -> bool:
                """True once the server has answered with a different page."""
                waited = 0.0
                while waited < STEP_DEADLINE_SECONDS:
                    self._sleep(1.0)
                    waited += 1.0
                    try:
                        if (page.url, page.content()) != (prev_url, prev_content):
                            return True
                    except error_type:
                        pass  # mid-navigation: keep polling until it lands
                return False

            def page_evidence() -> dict:
                """Best-effort server answer for the docker logs: WHAT the
                page says after a crash decides the next fix, no round-trip
                with the user needed (2026-11-07 lesson)."""
                text = ""
                with suppress(Exception):
                    text = page.evaluate(
                        "() => document.body.innerText.replace(/\\s+/g,' ').slice(0, 600)"
                    )
                return {
                    "url": str(getattr(page, "url", ""))[:120],
                    "page_text": text,
                }

            def advance() -> bool:
                """Native-submit the current wizard step; False if unadvanceable."""
                prev_url, prev_content = page.url, read_content()
                # NO radio click here: the row's click handler re-fetches the
                # picture captcha and invalidates a code just OCR'd (live NAS
                # incident 2026-11-07: 3 correct keras answers, all rejected).
                # Train selection belongs BEFORE the OCR fetch -- see the gate.
                if not page.evaluate(_JS_ADVANCE_FORM):
                    return False
                # A submit was issued: whether the server answered or stayed
                # silent, only the page can now tell us if a ticket exists.
                settle(prev_url, prev_content)
                return True

            clicked_any = False
            captcha_budget = self.captcha_retries
            last_url = ""
            try:
                for _step in range(WIZARD_STEPS):
                    content = read_content()
                    url_now = page.url
                    gate = (
                        self.captcha_solver is not None
                        and page_shows_captcha(content)
                        and self._first_visible(page, CAPTCHA_INPUT_SELECTORS) is not None
                    )
                    if (
                        self.captcha_solver is None
                        and page_shows_captcha(content)
                        and self._first_visible(page, CAPTCHA_INPUT_SELECTORS) is not None
                    ):
                        # Live incident 2026-11-07 (NAS): OCR deps silently
                        # missing in the image meant the gate was never even
                        # attempted; blind re-submits burned the wizard. A
                        # captcha page proves the server booked nothing, so
                        # this is a CLEAN failure the scheduler may re-queue.
                        return SubmitResult(
                            False, reason="圖形驗證碼關卡，但此容器沒有可用的 OCR（鏡像 OCR 依賴缺失）"
                        )
                    outcome = parse_booking_outcome(content)
                    if outcome.ok:
                        return outcome
                    # A wrong picture code also reads as 驗證失敗; when the
                    # captcha gate is up that is this round's retry path, not
                    # a verdict. Anything else the official page reports is.
                    if (
                        "官方頁面回報" in outcome.reason
                        and not (gate and "驗證失敗" in outcome.reason)
                    ):
                        return outcome
                    # Select the row once per fresh page: its click handler
                    # re-fetches the captcha, so it must never sit between a
                    # solved code and the submit (replay-proven order).
                    if url_now != last_url:
                        last_url = url_now
                        with suppress(error_type):
                            page.evaluate(_JS_SELECT_TRAIN)
                            self._human_pause(0.3, 0.7)  # let the captcha reload land
                    if gate:
                        if captcha_budget <= 0:
                            # The server still asks for a picture code: it
                            # booked NOTHING (the code is a precondition), so
                            # a clean failure -- the next poll tick retries
                            # with fresh captcha odds. unknown would freeze
                            # the task forever after three unlucky OCR reads.
                            return SubmitResult(
                                False, reason="圖形驗證碼連續未通過，下一輪自動重試"
                            )
                        captcha_budget -= 1
                        # Solve AND submit in one shot (replay order): the
                        # atomic last-tick inside _solve_captcha re-checks the
                        # row, burns the v3 token and native-submits without
                        # ever releasing the solved code back to the page.
                        try:
                            solved = self._solve_captcha(page, prefill, submit=True)
                        except error_type:
                            solved = False  # stale page: advance() re-reads
                        if solved:
                            clicked_any = True
                            # An accepted native submit means navigation:
                            # read the page only after it lands (live NAS
                            # 2026-11-07: re-reading mid-navigation raised
                            # 'Unable to retrieve content' and the round
                            # died as a clean failure -> every-minute
                            # re-queue would book again).
                            with suppress(Exception):
                                page.wait_for_load_state(
                                    "load", timeout=STEP_DEADLINE_SECONDS * 1000
                                )
                            continue
                    try:
                        # advance() first: `clicked_any or advance()` would
                        # short-circuit and never submit past the first step.
                        clicked_any = advance() or clicked_any
                    except error_type as exc:
                        # A raised submit may still have been accepted by the
                        # server: a retry could book a second real, paid ticket.
                        logger.warning(
                            "auto-submit browser error after a submit; outcome unknown",
                            extra={"event": "autobook.browser_error_post_click",
                                   **page_evidence()},
                            exc_info=True,
                        )
                        message = str(exc).splitlines()[0] if str(exc) else "送出後瀏覽器錯誤"
                        return SubmitResult(False, unknown=True, reason=f"瀏覽器錯誤：{message}")
                outcome = parse_booking_outcome(read_content())
                if outcome.ok or "官方頁面回報" in outcome.reason:
                    return outcome
                logger.warning(
                    "auto-submit stopped unreadable",
                    extra={
                        "event": "autobook.unreadable",
                        "url": page.url[:120],
                        "page_text": page.evaluate(
                            "() => document.body.innerText.replace(/\\s+/g,' ').slice(0, 600)"
                        ),
                    },
                )
                # The wizard ran and the final page cannot be read: unknown.
                # Never re-queue a blind submit -- it could book twice.
                return SubmitResult(
                    False, unknown=True, reason=outcome.reason or "等了很久仍讀不到訂票結果"
                ) if clicked_any else SubmitResult(
                    False, reason="找不到可送出的表單（頁面可能已改版）"
                )
            except error_type as exc:
                # The invariant: once any submit has left the browser, only
                # the official page can say whether a ticket exists. A crash
                # here (navigation races included) must park as unknown --
                # a clean failure re-queues every minute and books twice
                # (live NAS incident 2026-11-07 01:21).
                if not clicked_any:
                    raise
                logger.warning(
                    "auto-submit browser error after a submit; outcome unknown",
                    extra={"event": "autobook.browser_error_post_click",
                           **page_evidence()},
                    exc_info=True,
                )
                message = str(exc).splitlines()[0] if str(exc) else "送出後瀏覽器錯誤"
                return SubmitResult(False, unknown=True, reason=f"瀏覽器錯誤：{message}")
            finally:
                pass  # keeps the wizard try honest; browser closes outside
        finally:
            browser.close()

    def _solve_captcha(self, page: Any, prefill: dict | None = None, submit: bool = True) -> bool:
        """Read the picture code, refill the form and resubmit. True on success.

        The picture is screenshotted from the rendered page -- the exact
        pixels the server expects an answer for (an extra GET of its URL can
        return a different image than the one bound to the session).
        """
        import hashlib
        import os
        import tempfile

        assert self.captcha_solver is not None
        image = self._first_visible(page, CAPTCHA_IMG_SELECTORS)
        if image is None:
            return False
        try:
            # OCR the pixels the BROWSER shows, never a separately fetched
            # copy: the server binds the expected code to the picture that
            # rendered with the page, so an extra GET can return a different
            # image -- correct answers to the wrong picture (NAS incident
            # 2026-11-07 round two: three correct OCR hits, three rejects,
            # the gate re-rendered verbatim). A human reads the screen; so
            # do we.
            body = image.screenshot()
        except Exception:  # noqa: BLE001 - stale element: clean failure re-queues
            return False
        if not body or len(body) < 100:
            return False
        # sha256 of the exact OCR'd bytes: makes "server sent us the same
        # picture three times" (or different ones) provable from docker logs.
        digest = hashlib.sha256(body).hexdigest()[:12]
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
            # Distinguish "every OCR subprocess died" from "OCR answered but
            # the server refused the code": WARNING + tried-chain list makes
            # a dead chain visible in docker logs (2026-11-07 NAS lesson).
            logger.warning(
                "captcha OCR produced no code",
                extra={
                    "event": "autobook.captcha_ocr_empty",
                    "tried": "+".join(n for n, _ in self.captcha_solver.commands),
                    "stderr": getattr(self.captcha_solver, "last_stderr", "")[:300],
                    "digest": digest,
                },
            )
            return False
        logger.warning(
            "captcha OCR answered",
            extra={
                "event": "autobook.captcha_ocr_ok",
                "chain": self.captcha_solver.last_used,
                "len": len(code),
                "digest": digest,
            },
        )

        field = self._first_visible(page, CAPTCHA_INPUT_SELECTORS)
        if field is None:
            return False
        try:
            # Raw value set, NO events: #verifyCode is the g-recaptcha-response
            # field and the page's reCAPTCHA JS clears it on input events
            # (puppeteer replay proved a bare el.value survives to the server).
            field.evaluate("(el, v) => { el.value = v; }", code)
            check = field.evaluate("el => el.value")
            if check != code:
                return False
        except Exception:  # noqa: BLE001 - stale element means retry next tick
            return False
        type_field = page.query_selector(CAPTCHA_TYPE_FIELD)
        if type_field is not None:
            type_field.evaluate("el => { el.value = 'text'; }")
        if prefill and (prefill.get("values") or prefill.get("checked")):
            # The re-rendered page answers "輸入資料有誤" unless every field is
            # put back the instant before this click (run3.mjs pitfall).
            page.evaluate(_JS_RESTORE_FORM, [prefill.get("values", {}), prefill.get("checked", "")])
        # Picture-code mode is the server's second-verify path. The
        # replay-proven (two real tickets) handling is an UNCONDITIONAL clear:
        # grecaptcha.execute() leaves a scored-and-burned v3 token in this
        # field, and the server re-validates whatever rides along -- keeping
        # it made three correctly OCR'd codes get rejected in a loop (live
        # NAS incident 2026-11-07). Empty + enabled makes the server take the
        # picture-code branch. Our code lives in #verifyCode, never here.
        page.evaluate(
            """(code) => {
                document.querySelectorAll("input[name=g-recaptcha-response]").forEach(
                    i => { if (i.value !== code) { i.value = ''; } i.disabled = false; });
                const isv = document.querySelector("input[name=isSecondVerify]");
                if (isv) isv.value = 'true';
            }""",
            code,
        )
        if not submit:
            return True
        # Last-tick pass, verbatim from the replay that booked real tickets:
        # radio re-check, token burn, code refill and native form.submit() in
        # ONE evaluate -- nothing (captcha re-fetch, page JS, round-trips)
        # can slip between the filled code and the request leaving the
        # browser. Falls back to the visible button on pages with no
        # selectLoc form (other wizard steps).
        try:
            submitted = bool(
                page.evaluate(
                    """(code) => {
                        const rows = [...document.querySelectorAll("input[name^='selectLoc']")];
                        if (!rows.length) return false;
                        const hidden = document.querySelector("input[name*='trainNoList']");
                        const want = hidden ? String(hidden.value || '').trim() : '';
                        const tg = rows.find(r => want && (r.closest('tr')?.innerText || '').includes(want)) || rows[0];
                        if (tg && !tg.checked) tg.click();
                        document.querySelectorAll("input[name=g-recaptcha-response]").forEach(
                            i => { if (i.value !== code) { i.value = ''; } i.disabled = false; });
                        const vc = document.querySelector("#verifyCode"); if (vc) vc.value = code;
                        const isv = document.querySelector("input[name=isSecondVerify]"); if (isv) isv.value = 'true';
                        const vt = document.querySelector("input[name=verifyType]"); if (vt) vt.value = 'text';
                        const f = [...document.querySelectorAll("form")].find(x => x.querySelector("input[name^='selectLoc']"));
                        if (f) { f.submit(); return true; }
                        return false;
                    }""",
                    code,
                )
            )
        except Exception:  # noqa: BLE001 - stale page; button fallback below
            submitted = False
        if submitted:
            return True
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
    """Keras CRNN first, ddddocr second; only entries installed here count.

    Outside this host (e.g. inside the API container) the venv paths will not
    exist; a ddddocr importable by the running interpreter is then used so
    picture-code OCR travels with the image instead of the filesystem.
    """
    import os
    import sys
    from pathlib import Path

    root = Path(os.getenv("TRA_OCR_HOME", "/home/hermesbot/projects/tra_ocr"))
    commands = []
    keras_py = os.getenv("TRA_AUTOBOOK_KERAS_PY", str(root / "kerasvenv/bin/python"))
    keras_script = os.getenv("TRA_AUTOBOOK_KERAS_SCRIPT", str(root / "data/tra_predict.py"))
    if Path(keras_py).exists() and Path(keras_script).exists():
        commands.append(("keras", [keras_py, keras_script]))
    dddd_py = os.getenv("TRA_AUTOBOOK_DDDD_PY", str(root / "ocrvenv/bin/python"))
    if Path(dddd_py).exists():
        dddd_python = dddd_py
    else:
        try:
            import ddddocr  # noqa: F401
        except ImportError:
            dddd_python = ""
        else:
            dddd_python = sys.executable
    if dddd_python:
        commands.append(("ddddocr", [dddd_python, "-c",
            "import sys,ddddocr; print(ddddocr.DdddOcr(show_ad=False).classification(open(sys.argv[1],'rb').read()))"]))
    # Baked CRNN: weights ship inside the package, so a tensorflow importable
    # by THIS interpreter is all the image needs (no TRA_OCR_HOME mount).
    # THSR-Sniper pattern: model in git, deps in the image, zero host venvs.
    import importlib.util

    baked = Path(__file__).parent / "_ocr/tra_predict.py"
    if importlib.util.find_spec("tensorflow") and baked.exists():
        commands.insert(0, ("keras-baked", [sys.executable, str(baked)]))
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
    solver = build_captcha_solver_from_env()
    # State the OCR chain out loud at startup: a parked captcha round is
    # ambiguous without it (baked OCR dead vs codes OCR'd wrong 3x).
    # WARNING, not INFO: this runs before configure_logging() attaches the
    # handler, and INFO was silently swallowed (live miss 2026-11-07 NAS --
    # the armed line never appeared even on the right image).
    logger.warning(
        "auto-submit armed",
        extra={
            "event": "autobook.armed",
            "captcha": "+".join(name for name, _ in solver.commands) if solver else "none",
        },
    )
    return PlaywrightAutoSubmitter(
        headless=headless,
        timeout_ms=timeout_ms,
        captcha_solver=solver,
        captcha_retries=retries,
    )
