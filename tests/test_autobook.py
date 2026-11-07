"""Auto-submit mode (reCAPTCHA v3 via headless browser).

The browser itself is never launched here: parse_booking_outcome is pure and
the scheduler runs against a stub submitter, so the suite stays fast and does
not require the optional playwright extra. The picture-code fallback (the
official response when v3 scores a datacenter IP zero) is covered with a fake
OCR chain and a duck-typed fake page.
"""

import threading
import time
import typing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.autobook import (
    _JS_ADVANCE_FORM,
    _JS_RESTORE_FORM,
    OcrChain,
    PlaywrightAutoSubmitter,
    SubmitResult,
    page_shows_captcha,
    parse_booking_outcome,
)
from tra_sniper.models import BookingRequest
from tra_sniper.notifications import WebhookNotifier
from tra_sniper.scheduler import MAX_AUTOBOOK_FAILURES, TaskScheduler
from tra_sniper.storage import (
    MODE_AUTO_SUBMIT,
    POLLABLE_STATUSES,
    Database,
)

OFFICIAL = "https://maas.transportdata.tw/trip/api/v1/booking/deeplink/redirect?token=t-1"


def test_parser_reads_success_with_code() -> None:
    result = parse_booking_outcome("恭喜，訂票成功！請记下電腦代碼 A1234567 并于三日内缴费")
    assert result.ok
    assert result.booking_code == "A1234567"


def test_parser_handles_success_without_code() -> None:
    result = parse_booking_outcome("預約成功")
    assert result.ok
    assert result.booking_code is None


def test_parser_reads_failure_pages() -> None:
    result = parse_booking_outcome("訂票失敗：此車次已售完")
    assert not result.ok
    assert "訂票失敗" in result.reason
    sold_out = parse_booking_outcome("此車次已售完，請改選其他時段")
    assert not sold_out.ok
    assert "已售完" in sold_out.reason


def test_parser_flags_unknown_pages_as_not_understood() -> None:
    result = parse_booking_outcome("請先選擇乘車日期")
    assert not result.ok
    assert "改版" in result.reason


class StubDb:
    """The scheduler surface _try_autobook touches, with an assertion journal.

    Mechanical adaptation for the hardening spec: update_task_status accepts
    the new keyword-only only_from guard, and the strike/park/list helpers the
    scheduler now calls exist here as in-memory equivalents. No existing
    assertion changed.
    """

    def __init__(self, payload: dict) -> None:
        self.task = SimpleNamespace(id="t1", user_id=1, status="monitoring", mode=MODE_AUTO_SUBMIT)
        self.payload = payload
        self.statuses: list[tuple[str, str | None]] = []
        self.check_failures = 0
        self.parked: list[tuple[str, str]] = []

    def expire_finished_monitors(self):
        return []

    def claim_due_checks(self):
        return [self.task]

    def get_task_payload(self, task_id, user_id):
        return self.payload

    def get_task(self, task_id, user_id):
        return self.task

    def update_task_status(
        self, task_id, user_id, status, *, last_error=None, booking_code=None, only_from=None
    ):
        if only_from is not None and self.task.status not in only_from:
            return False
        self.statuses.append((status, booking_code))
        self.task = SimpleNamespace(
            id=task_id, user_id=user_id, status=status, mode=self.task.mode,
            booking_code=booking_code, last_error=last_error,
        )
        return True

    def pause_monitoring(self, task_id, user_id, status):
        self.statuses.append((status, None))
        return True

    def park_for_human_review(self, task_id, user_id, *, last_error):
        self.parked.append((task_id, last_error))
        self.statuses.append(("waiting_human", None))
        self.task = SimpleNamespace(
            id=task_id, user_id=user_id, status="waiting_human", mode=self.task.mode,
            booking_code=None, last_error=last_error,
        )
        return True

    def record_check_failure(self, task_id):
        self.check_failures += 1
        return self.check_failures

    def clear_check_failures(self, task_id):
        self.check_failures = 0

    def list_tasks_by_status(self, status):
        return [self.task] if self.task.status == status else []


class StubSubmitter:
    def __init__(self, result: SubmitResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def submit(self, url, identity):
        self.calls.append((url, identity))
        return self.result


class RecordingNotifier(WebhookNotifier):
    enabled = True  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.reminded: list[str] = []
        self.results: list[tuple[str, str, str | None]] = []

    def notify(self, task, payload):
        self.reminded.append(task.id)
        return True

    def notify_result(self, task, status, code):
        self.results.append((task.id, status, code))
        return True


def _scheduler(db, submitter) -> TaskScheduler:
    notifier = RecordingNotifier()
    notifier.official_url_for = lambda task: OFFICIAL
    return TaskScheduler(db, notifier=notifier, autobook=submitter)  # type: ignore[arg-type]


def _wait_for_browser_rounds() -> None:
    for thread in threading.enumerate():
        if thread.name.startswith("tra-autobook-"):
            thread.join(timeout=5)


def test_auto_submit_books_completes_task_without_reminder() -> None:
    db = StubDb({"identity": "A123456789"})
    submitter = StubSubmitter(SubmitResult(True, booking_code="A1234567"))
    scheduler = _scheduler(db, submitter)
    scheduler.tick()
    _wait_for_browser_rounds()
    assert submitter.calls == [(OFFICIAL, "A123456789")]
    assert ("completed", "A1234567") in db.statuses
    assert scheduler.notifier.reminded == []  # booked; no reminder needed


def test_auto_submit_failure_falls_back_to_reminder() -> None:
    db = StubDb({"identity": "A123456789"})
    submitter = StubSubmitter(SubmitResult(False, reason="已售完"))
    scheduler = _scheduler(db, submitter)
    scheduler.tick()
    _wait_for_browser_rounds()
    assert scheduler.notifier.reminded == ["t1"]  # the reminder covers for the browser
    assert not any(status == "completed" for status, _ in db.statuses)


def test_auto_submit_without_identity_reminds_and_explains() -> None:
    db = StubDb({"identity": ""})
    submitter = StubSubmitter(SubmitResult(True))
    scheduler = _scheduler(db, submitter)
    scheduler.tick()
    _wait_for_browser_rounds()
    assert scheduler.notifier.reminded == ["t1"]
    assert submitter.calls == []  # never sends an empty ID to the page


def test_slow_browser_round_does_not_block_the_tick() -> None:
    release = threading.Event()

    class SlowSubmitter(StubSubmitter):
        def submit(self, url, identity):
            self.calls.append((url, identity))
            release.wait(5)
            return self.result

    db = StubDb({"identity": "A123456789"})
    submitter = SlowSubmitter(SubmitResult(False, reason="已售完"))
    scheduler = _scheduler(db, submitter)
    started = time.monotonic()
    scheduler.tick()
    scheduler.tick()  # next round while the browser is still busy
    assert time.monotonic() - started < 1
    release.set()
    _wait_for_browser_rounds()
    assert len(submitter.calls) == 1  # no second browser for the same task
    assert scheduler.notifier.reminded == ["t1"]


def test_round_skips_task_cancelled_while_queued() -> None:
    db = StubDb({"identity": "A123456789"})
    submitter = StubSubmitter(SubmitResult(True))
    scheduler = _scheduler(db, submitter)
    db.task = SimpleNamespace(**{**vars(db.task), "status": "cancelled"})
    scheduler._autobook_round(db.task)
    assert submitter.calls == []
    assert scheduler.notifier.reminded == []


def test_auto_submit_without_submitter_behaves_like_plain_mode() -> None:
    db = StubDb({"identity": "A123456789"})
    scheduler = TaskScheduler(db, notifier=WebhookNotifier(), autobook=None)  # type: ignore[arg-type]
    assert scheduler.tick() == 1


def _booking() -> dict:
    return {
        "identity": "A123456789",
        "start_station": "1000-臺北",
        "end_station": "4220-臺南",
        "quantity": 1,
        "outbound": {"ride_date": "2099/01/01", "train_numbers": ["152"]},
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TRA_AUTOBOOK_ENABLED", raising=False)
    monkeypatch.setenv("TRA_TOKEN_SECRET", "x" * 32)
    db = Database(tmp_path / "tra.db", encryption_key=Fernet.generate_key().decode())

    class FakeTdx:
        configured = True

        def stations(self, fallback):
            return list(fallback)

        def load_cached_stations(self):
            return []

        def booking_link(self, *args):
            return OFFICIAL

    submitter = StubSubmitter(SubmitResult(True, booking_code="A1234567"))
    app = create_app(database=db, token_manager=TokenManager(), tdx_client=FakeTdx(),
                     start_scheduler=False, autobook=submitter)
    with TestClient(app) as client:
        client.submitter = submitter
        yield client


def _register(client) -> dict:
    client.post("/auth/register", json={"email": "a@b.com", "password": "secret-password-1"})
    login = client.post("/auth/login", json={"email": "a@b.com", "password": "secret-password-1"}).json()
    return {"Authorization": f"Bearer {login['access_token']}"}


def test_api_accepts_auto_submit_mode(client) -> None:
    headers = _register(client)
    response = client.post(
        "/tasks",
        json={"booking": _booking(), "mode": MODE_AUTO_SUBMIT, "train_label": "152"},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["mode"] == "auto_submit"


def test_api_autobook_try_books_and_completes(client) -> None:
    headers = _register(client)
    task = client.post(
        "/tasks",
        json={"booking": _booking(), "mode": MODE_AUTO_SUBMIT, "train_label": "152"},
        headers=headers,
    ).json()
    result = client.post(f"/tasks/{task['id']}/autobook/try", headers=headers)
    assert result.status_code == 200
    body = result.json()
    assert body["ok"] is True
    assert body["booking_code"] == "A1234567"
    updated = client.get("/tasks", headers=headers).json()[0]
    assert updated["status"] == "completed"
    # Trying again on a booked task must not book a second ticket.
    again = client.post(f"/tasks/{task['id']}/autobook/try", headers=headers).json()
    assert again["ok"] is False
    assert len(client.submitter.calls) == 1


def test_try_now_refuses_while_a_round_is_running() -> None:
    db = StubDb({"identity": "A123456789"})
    submitter = StubSubmitter(SubmitResult(True))
    scheduler = _scheduler(db, submitter)
    assert scheduler._claim("t1")  # a scheduled round holds the task
    assert scheduler.autobook_now("t1", 1, OFFICIAL, "A123456789") is None
    assert submitter.calls == []
    scheduler._release("t1")
    assert scheduler.autobook_now("t1", 1, OFFICIAL, "A123456789").ok


# ---- picture-code fallback ---------------------------------------------------


class FakeChain:
    def __init__(self, code: str | None) -> None:
        self.code = code
        self.calls = 0
        self.commands = [("fake", ["/bin/fake"])]  # OcrChain-shaped for the logs
        self.last_used = ""

    def solve(self, image_path):
        self.calls += 1
        if self.code:
            self.last_used = "fake"
        return self.code


class FakeElement:
    def __init__(self, src=None, visible=True) -> None:
        self.src = src
        self._visible = visible
        self.filled = None
        self.clicked = False
        self.evaluated = None
        self.value = None

    def is_visible(self):
        return self._visible

    def get_attribute(self, name):
        return self.src

    def fill(self, value, timeout=None):
        self.filled = value

    def click(self):
        self.clicked = True

    def evaluate(self, script, arg=None):
        self.evaluated = script
        if script.startswith("(el, v)"):   # raw value set, no events (reCAPTCHA pitfall)
            self.value = arg
            return None
        if script == "el => el.value":
            return self.value
        return None


class FakeResponse:
    def body(self):
        return b"fake-jpeg-bytes"


class FakeRequest:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url):
        self.urls.append(url)
        return FakeResponse()


class FakePage:
    url = "https://tb.trans.railway.gov.tw/tra-tip-web/tip/tip001/tip121/booking"

    def __init__(self) -> None:
        self.picture = FakeElement(src="/tra-tip-web/tip/player/picture?r=99")
        self.verify = FakeElement()
        self.verify_type = FakeElement()
        self.button = FakeElement()
        self.request = FakeRequest()
        self.evaluates: list[tuple[str, object]] = []

    def evaluate(self, script, arg=None):
        self.evaluates.append((script, arg))
        if "arrayBuffer" in script:  # in-page picture fetch (session cookies ride along)
            import base64

            return base64.b64encode(b"fake-jpeg-bytes").decode()
        return None

    def query_selector(self, selector):
        return {
            "img[src*='player/picture']": self.picture,
            "#verifyCode": self.verify,
            "input[name='verifyType']": self.verify_type,
            "button:has-text('確認訂票')": self.button,
        }.get(selector)


def test_ocr_chain_prefers_primary_and_falls_back() -> None:
    results = [
        SimpleNamespace(returncode=1, stdout=""),          # keras dies
        SimpleNamespace(returncode=0, stdout="24p8k7\n"),  # ddddocr saves it
    ]
    chain = OcrChain(
        [("keras", ["/bin/keras"]), ("ddddocr", ["/bin/dddd"])],
        runner=lambda argv, timeout: results.pop(0),
    )
    assert chain.solve("x.jpg") == "24P8K7"  # TRA is case-insensitive: upper is fine
    assert chain.last_used == "ddddocr"


def test_ocr_chain_rejects_short_noise() -> None:
    chain = OcrChain(
        [("keras", ["/bin/keras"])],
        runner=lambda argv, timeout: SimpleNamespace(returncode=0, stdout="ab\n"),
    )
    assert chain.solve("x.jpg") is None


def test_captcha_chain_prefers_the_baked_crnn_when_tensorflow_is_present(monkeypatch, tmp_path) -> None:
    # The THSR-Sniper deployment shape: no host venvs at all, tensorflow and
    # the in-package weights are everything the image has.
    import importlib.util as ilu

    monkeypatch.setenv("TRA_OCR_HOME", str(tmp_path / "absent"))  # no kerasvenv/ocrvenv
    monkeypatch.setenv("TRA_AUTOBOOK_DDDD_PY", str(tmp_path / "no-dddd"))
    monkeypatch.setattr(ilu, "find_spec",
                        lambda name: object() if name == "tensorflow" else None)
    monkeypatch.setitem(__import__("sys").modules, "ddddocr", None)  # no importable ddddocr

    from tra_sniper.autobook import build_captcha_solver_from_env

    chain = build_captcha_solver_from_env()
    assert chain is not None
    name, argv = chain.commands[0]
    assert name == "keras-baked" and argv[-1].endswith("_ocr/tra_predict.py")


def test_captcha_chain_is_none_without_any_ocr(monkeypatch, tmp_path) -> None:
    import importlib.util as ilu

    monkeypatch.setenv("TRA_OCR_HOME", str(tmp_path / "absent"))
    monkeypatch.setenv("TRA_AUTOBOOK_DDDD_PY", str(tmp_path / "no-dddd"))
    monkeypatch.setattr(ilu, "find_spec", lambda name: None)
    monkeypatch.setitem(__import__("sys").modules, "ddddocr", None)

    from tra_sniper.autobook import build_captcha_solver_from_env

    assert build_captcha_solver_from_env() is None


def test_page_shows_captcha_marker() -> None:
    assert page_shows_captcha("請輸入下圖驗證碼")
    assert not page_shows_captcha("訂票成功")


def test_parse_booking_outcome_reads_the_live_tip115_page() -> None:
    # Verbatim shape of the 2026-11-07 end-to-end success page: the code is
    # PLAIN 7 digits next to 訂票代碼 -- a letters-only pattern missed it.
    text = (
        "訂票人N7654**** 票種與加購台鐵便當 訂票成功！ "
        "請選擇票種/付款方式，並於 11/07 17:33 (Sat) 前完成付款。 "
        "訂票代碼 2854451 行程訂購資訊 11/07(星期六) 17:53 新竹 到 18:52 大甲 "
        "自強(3000) 179 大學生5折 1 237 4車51號 全票 237 元 下一步"
    )
    result = parse_booking_outcome(text)
    assert result.ok and result.booking_code == "2854451"


def test_parse_booking_reports_bad_credential_as_clean_failure() -> None:
    # Live 2026-11-07 fake-id probe: the server answers "身分證字號格式錯誤"
    # after the submit. That is a known identity failure (no ticket booked), so
    # it must be a CLEAN failure -- unknown would wrongly freeze re-tries.
    result = parse_booking_outcome("輸入資料有誤，請依下方提示檢查後重新填寫。 身分證字號格式錯誤")
    assert (result.ok, result.unknown) == (False, False)
    assert "身分證字號格式錯誤" in result.reason


def test_resolve_deeplink_passes_through_non_deeplinks_and_failures() -> None:
    from tra_sniper.autobook import _resolve_deeplink

    plain = "https://www.trc.com.tw/tra-tip-web/tip/tip001/tip123/query"
    assert _resolve_deeplink(plain) == plain  # not a deeplink: untouched
    dead = "http://127.0.0.1:1/tra-tip-web/deeplink/redirect?token=x"
    assert _resolve_deeplink(dead) == dead  # unresolvable: still worth a browser try


def test_captcha_image_url_cache_buster_is_random() -> None:
    # NAS incident 2026-11-07: cache-buster was os.getpid(), uvicorn in the
    # container is ALWAYS pid 1, every round re-fetched one cached picture
    # while the server's expected code rotated -- 9 correct OCR hits, 0 pass.
    urls = []
    for _ in range(2):
        page = FakePage()
        submitter = PlaywrightAutoSubmitter(
            captcha_solver=FakeChain("AB3D9F"), sleeper=lambda seconds: None
        )
        assert submitter._solve_captcha(page) is True
        urls.extend(str(a) for s, a in page.evaluates if "player/picture" in str(a))
    assert len(urls) == 2 and urls[0] != urls[1]


def test_solve_captcha_reads_image_fills_and_resubmits() -> None:
    page = FakePage()
    submitter = PlaywrightAutoSubmitter(
        captcha_solver=FakeChain("AB3D9F"), sleeper=lambda seconds: None
    )
    assert submitter._solve_captcha(page) is True
    # the picture is fetched inside the page so session cookies ride along
    assert any("/tra-tip-web/tip/player/picture" in str(a) for _, a in page.evaluates)
    assert page.verify.value == "AB3D9F"
    assert page.verify_type.evaluated is not None  # verifyType set to 'text'
    assert page.button.clicked is True


def test_solve_captcha_refills_wiped_form_before_resubmit() -> None:
    # Live-verified failure mode: the server re-render wipes train/date/pid, so
    # a resubmit that does not restore them only ever answers "輸入資料有誤".
    page = FakePage()
    submitter = PlaywrightAutoSubmitter(
        captcha_solver=FakeChain("AB3D9F"), sleeper=lambda seconds: None
    )
    prefill = {
        "values": {'input[name="ticketOrderParamList[0].trainNoList[0]"]': "179"},
        "checked": "PERSON_ID",
    }
    assert submitter._solve_captcha(page, prefill) is True
    restore_scripts = [(s, a) for s, a in page.evaluates if "custIdTypeEnum" in s]
    assert restore_scripts == [(
        _JS_RESTORE_FORM,
        [{'input[name="ticketOrderParamList[0].trainNoList[0]"]': "179"}, "PERSON_ID"],
    )]
    second_verify = [s for s, _ in page.evaluates if "isSecondVerify" in s]
    assert second_verify, "empty v3 token must not ride along on resubmit"


def test_solve_captcha_gives_up_when_field_goes_stale() -> None:
    # Navigation mid-solve must cost one tick, not the whole round budget.
    page = FakePage()

    def boom(script, arg=None):
        raise RuntimeError("ElementHandle.evaluate: Timeout 30000ms exceeded.")

    page.verify.evaluate = boom
    submitter = PlaywrightAutoSubmitter(
        captcha_solver=FakeChain("AB3D9F"), sleeper=lambda seconds: None
    )
    assert submitter._solve_captcha(page, {"values": {}, "checked": ""}) is False
    assert page.button.clicked is False


def test_solve_captcha_without_readable_code_gives_up_quietly() -> None:
    page = FakePage()
    submitter = PlaywrightAutoSubmitter(
        captcha_solver=FakeChain(None), sleeper=lambda seconds: None
    )
    assert submitter._solve_captcha(page) is False
    assert page.verify.filled is None


# ---- hardening spec: never re-book an unknown outcome (R1) -------------------


def _scheduler_with(db, submitter, notifier) -> TaskScheduler:
    notifier.official_url_for = lambda task: OFFICIAL
    return TaskScheduler(db, notifier=notifier, autobook=submitter)  # type: ignore[arg-type]


def test_submit_result_as_dict_carries_unknown() -> None:
    assert SubmitResult(False).unknown is False
    assert SubmitResult(False, unknown=True, reason="x").as_dict() == {
        "ok": False, "booking_code": None, "reason": "x", "unknown": True,
    }


def test_sentinel_written_before_submit_and_clean_result_reverts() -> None:
    db = StubDb({"identity": "A123456789"})
    seen_at_submit: list[str] = []

    class PeekingSubmitter(StubSubmitter):
        def submit(self, url, identity):
            seen_at_submit.append(db.task.status)
            return super().submit(url, identity)

    submitter = PeekingSubmitter(SubmitResult(False, reason="已售完"))
    scheduler = _scheduler_with(db, submitter, RecordingNotifier())
    scheduler.tick()
    _wait_for_browser_rounds()
    # The durable trace exists before the click can be accepted...
    assert seen_at_submit == ["autobooking"]
    # ...and a clean (understood) result puts the task back in the poll loop.
    assert db.task.status == "monitoring"
    # The poll loop must never hand out an 'autobooking' task.
    assert "autobooking" not in POLLABLE_STATUSES


def test_stale_autobooking_task_is_never_claimed(tmp_path) -> None:
    db = Database(tmp_path / "sentinel.db", encryption_key=Fernet.generate_key().decode())
    user = db.create_user("a@b.com", "hash")
    now = datetime.now(UTC)
    task = db.create_task(
        user.id, BookingRequest.from_dict(_booking()),
        (now - timedelta(minutes=5)).isoformat(), _booking(), mode=MODE_AUTO_SUBMIT,
    )
    db.claim_due_checks(now.isoformat())
    db.update_task_status(task.id, user.id, "autobooking")
    later = (now + timedelta(hours=1)).isoformat()
    assert db.claim_due_checks(later) == []


def _boom_submitter():
    class BoomSubmitter(StubSubmitter):
        def submit(self, url, identity):
            self.calls.append((url, identity))
            raise RuntimeError("browser died after the click")

    return BoomSubmitter(SubmitResult(True))


def test_raising_submitter_parks_for_human_and_notifies_unknown_once() -> None:
    db = StubDb({"identity": "A123456789"})
    notifier = RecordingNotifier()
    scheduler = _scheduler_with(db, _boom_submitter(), notifier)
    scheduler.tick()
    _wait_for_browser_rounds()
    assert db.task.status == "waiting_human"
    assert len(db.parked) == 1 and "自動訂票出錯" in db.parked[0][1]
    assert notifier.results == [("t1", "unknown", None)]
    assert notifier.reminded == []  # parked, never silently re-queued


def test_unknown_submit_result_parks_for_human_and_notifies_unknown_once() -> None:
    db = StubDb({"identity": "A123456789"})
    notifier = RecordingNotifier()
    submitter = StubSubmitter(SubmitResult(False, unknown=True, reason="讀不到訂票結果（頁面可能已改版）"))
    scheduler = _scheduler_with(db, submitter, notifier)
    scheduler.tick()
    _wait_for_browser_rounds()
    assert submitter.calls == [(OFFICIAL, "A123456789")]
    assert db.task.status == "waiting_human"
    assert len(db.parked) == 1 and "自動訂票結果未知" in db.parked[0][1]
    assert notifier.results == [("t1", "unknown", None)]
    assert notifier.reminded == []


def test_try_now_unknown_parks_and_does_not_remind() -> None:
    db = StubDb({"identity": "A123456789"})
    notifier = RecordingNotifier()
    submitter = StubSubmitter(SubmitResult(False, unknown=True, reason="圖形驗證碼連續失敗"))
    scheduler = _scheduler_with(db, submitter, notifier)
    result = scheduler.autobook_now("t1", 1, OFFICIAL, "A123456789")
    assert result is not None and result.unknown and not result.ok
    assert db.task.status == "waiting_human"
    assert notifier.results == [("t1", "unknown", None)]
    assert notifier.reminded == []


def test_startup_recovery_parks_stale_autobooking_without_resubmitting(tmp_path) -> None:
    db = Database(tmp_path / "recover.db", encryption_key=Fernet.generate_key().decode())
    user = db.create_user("a@b.com", "hash")
    now = datetime.now(UTC)
    task = db.create_task(
        user.id, BookingRequest.from_dict(_booking()),
        (now - timedelta(minutes=5)).isoformat(), _booking(), mode=MODE_AUTO_SUBMIT,
    )
    db.claim_due_checks(now.isoformat())
    db.update_task_status(task.id, user.id, "autobooking")

    notifier = RecordingNotifier()
    submitter = StubSubmitter(SubmitResult(True, booking_code="A1234567"))
    scheduler = TaskScheduler(db, notifier=notifier, autobook=submitter, interval_seconds=300)
    assert scheduler.recover_unfinished_submissions() == 1
    parked = db.get_task(task.id, user.id)
    assert parked.status == "waiting_human"
    assert "進程曾中斷" in parked.last_error
    assert notifier.results == [(task.id, "unknown", None)]
    # The tick neither reminds nor re-books the parked task.
    assert scheduler.tick() == 0
    assert submitter.calls == []


def test_start_recovers_before_the_tick_loop(tmp_path) -> None:
    db = Database(tmp_path / "recover2.db", encryption_key=Fernet.generate_key().decode())
    user = db.create_user("a@b.com", "hash")
    now = datetime.now(UTC)
    task = db.create_task(
        user.id, BookingRequest.from_dict(_booking()),
        (now - timedelta(minutes=5)).isoformat(), _booking(), mode=MODE_AUTO_SUBMIT,
    )
    db.claim_due_checks(now.isoformat())
    db.update_task_status(task.id, user.id, "autobooking")
    scheduler = TaskScheduler(
        db, notifier=RecordingNotifier(),
        autobook=StubSubmitter(SubmitResult(True)), interval_seconds=3600,
    )
    scheduler.start()  # recovery runs synchronously, before the tick loop
    try:
        assert db.get_task(task.id, user.id).status == "waiting_human"
    finally:
        scheduler.stop()


# ---- round bail-out before the click (R2c) -----------------------------------


def test_round_rechecks_status_before_the_submit() -> None:
    db = StubDb({"identity": "A123456789"})
    reads = {"n": 0}
    original_get_task = db.get_task

    def get_task(task_id, user_id):
        reads["n"] += 1
        task = original_get_task(task_id, user_id)
        if reads["n"] >= 2:  # cancelled after the queue check, before the click
            return SimpleNamespace(
                id=task_id, user_id=user_id, status="cancelled", mode=MODE_AUTO_SUBMIT
            )
        return task

    db.get_task = get_task
    submitter = StubSubmitter(SubmitResult(True))
    scheduler = _scheduler_with(db, submitter, RecordingNotifier())
    scheduler._autobook_round(db.task)
    assert submitter.calls == []
    assert scheduler.notifier.reminded == []  # released everything, owed nothing


# ---- strike budget (R6) --------------------------------------------------------


def test_five_consecutive_failures_park_the_task_and_stop_submitting() -> None:
    db = StubDb({"identity": "A123456789"})
    notifier = RecordingNotifier()
    submitter = StubSubmitter(SubmitResult(False, reason="瀏覽器錯誤"))
    scheduler = _scheduler_with(db, submitter, notifier)
    for _ in range(MAX_AUTOBOOK_FAILURES + 2):
        db.task = SimpleNamespace(
            id="t1", user_id=1,
            # A parked task stays parked; the loop only re-arms while pollable.
            status="monitoring" if db.task.status not in ("waiting_human", "completed")
            else db.task.status,
            mode=MODE_AUTO_SUBMIT,
        )
        scheduler.tick()
        _wait_for_browser_rounds()
    assert len(submitter.calls) == MAX_AUTOBOOK_FAILURES  # no 6th submit
    assert db.task.status == "waiting_human"
    assert notifier.results == [("t1", "failed", None)]  # told the human once


def test_success_clears_the_failure_counter() -> None:
    db = StubDb({"identity": "A123456789"})
    notifier = RecordingNotifier()
    submitter = StubSubmitter(SubmitResult(False, reason="瀏覽器錯誤"))
    scheduler = _scheduler_with(db, submitter, notifier)
    for _ in range(2):
        scheduler.tick()
        _wait_for_browser_rounds()
    assert db.check_failures == 2
    submitter.result = SubmitResult(True, booking_code="A1234567")
    scheduler.tick()
    _wait_for_browser_rounds()
    assert db.task.status == "completed"
    assert db.check_failures == 0


def test_start_warns_when_autobook_has_no_notifier(caplog) -> None:
    import logging

    db = StubDb({"identity": "A123456789"})
    scheduler = TaskScheduler(
        db, notifier=WebhookNotifier(), autobook=StubSubmitter(SubmitResult(True)),
        interval_seconds=300,
    )
    with caplog.at_level(logging.WARNING, logger="tra_sniper.scheduler"):
        scheduler.start()
        scheduler.stop()
    assert "webhook" in caplog.text


# ---- manual try-now reminds on failure (R10) -----------------------------------


def test_try_now_failure_sends_exactly_one_reminder() -> None:
    db = StubDb({"identity": "A123456789"})
    notifier = RecordingNotifier()
    submitter = StubSubmitter(SubmitResult(False, reason="已售完"))
    scheduler = _scheduler_with(db, submitter, notifier)
    result = scheduler.autobook_now("t1", 1, OFFICIAL, "A123456789")
    assert result is not None and not result.ok and not result.unknown
    assert notifier.reminded == ["t1"]
    assert notifier.results == []
    assert db.task.status == "monitoring"  # sentinel reverted, still pollable


# ---- post-click unknown inside the submitter itself (R1b) ----------------------


def _fake_playwright(monkeypatch, page, error_type=RuntimeError):
    import sys
    import types

    class FakeBrowser:
        def close(self):
            pass

        def new_context(self, **kwargs):
            return FakeContext()

    class FakeContext:
        def set_default_timeout(self, timeout):
            pass

        def add_init_script(self, script):
            pass

        def new_page(self):
            return page

    class FakePlaywright:
        class chromium:  # playwright's own spelling
            @staticmethod
            def launch(**kwargs):
                return FakeBrowser()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    module = types.ModuleType("playwright.sync_api")
    module.Error = error_type
    module.sync_playwright = lambda: FakePlaywright()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)


class _Clickable:
    def __init__(self):
        self.clicked = False

    def is_visible(self):
        return True

    def click(self):
        self.clicked = True

    def type(self, text, delay=0):
        pass


class _DeadPage:
    def __init__(self, contents):
        self._contents = list(contents)
        self.identity = _Clickable()
        self.button = _Clickable()
        self.verify = _Clickable()
        self.submitted = False
        self.submit_raises = None
        self.evaluates: list[tuple[str, object]] = []
        self.url = "https://www.trc.com.tw/tra-tip-web/tip/tip001/tip123/query"
        self.show_captcha_field = False

    def goto(self, url, **kwargs):
        pass

    def content(self):
        if len(self._contents) > 1:
            return self._contents.pop(0)
        return self._contents[-1]

    def query_selector(self, selector):
        if selector == "input#pid":
            return self.identity
        if selector == "form#queryForm button[type=submit]":
            return self.button
        if selector == "#verifyCode" and self.show_captcha_field:
            return self.verify
        return None

    def evaluate(self, script, arg=None):
        self.evaluates.append((script, arg))
        if script is _JS_ADVANCE_FORM:
            if self.submit_raises is not None:
                raise self.submit_raises
            self.submitted = True
            return True
        return None


def test_post_click_unreadable_page_returns_unknown_not_raise(monkeypatch) -> None:
    page = _DeadPage(["<html>請先選擇乘車日期</html>"])
    _fake_playwright(monkeypatch, page, error_type=RuntimeError)
    submitter = PlaywrightAutoSubmitter(
        timeout_ms=10, sleeper=lambda seconds: None, captcha_solver=None
    )
    result = submitter.submit(OFFICIAL, "A123456789")
    assert (result.ok, result.unknown) == (False, True)
    assert "訂票結果" in result.reason
    assert page.submitted  # the native submit really happened


def test_captcha_gate_without_ocr_is_a_clean_failure(monkeypatch) -> None:
    # Live NAS incident 2026-11-07: ddddocr failed to import (slim image had
    # no libgl), captcha_solver was silently None, the gate was never
    # attempted, and the wizard burned every step blind. It must fail clean,
    # with a reason naming OCR -- and must NOT submit past a captcha gate.
    page = _DeadPage(["<html>因 v3 驗證未通過，請輸入驗證碼 請輸入驗證碼</html>"])
    page.show_captcha_field = True
    _fake_playwright(monkeypatch, page, error_type=RuntimeError)
    submitter = PlaywrightAutoSubmitter(timeout_ms=10, sleeper=lambda seconds: None)
    result = submitter.submit(OFFICIAL, "A123456789")
    assert (result.ok, result.unknown) == (False, False)
    assert "OCR" in result.reason
    assert not page.submitted  # never submits past an unsolved picture code


def test_captcha_strikes_out_requeues_instead_of_freezing(monkeypatch) -> None:
    # A server still asking for a picture code booked NOTHING (code is a
    # precondition), so three failed OCR reads must stay re-queueable.
    page = _DeadPage(["<html>因 v3 驗證未通過 請輸入驗證碼</html>"])
    page.show_captcha_field = True
    _fake_playwright(monkeypatch, page, error_type=RuntimeError)

    class NeverSolves:
        commands: typing.ClassVar = [("never", ["/bin/never"])]
        last_used: typing.ClassVar = ""

        def solve(self, image_path):
            return None

    submitter = PlaywrightAutoSubmitter(
        timeout_ms=10, sleeper=lambda seconds: None,
        captcha_solver=NeverSolves(), captcha_retries=2,
    )
    result = submitter.submit(OFFICIAL, "A123456789")
    assert (result.ok, result.unknown) == (False, False)
    assert "驗證碼" in result.reason


def test_post_click_captcha_strikes_out_as_unknown(monkeypatch) -> None:
    # The loop sees the picture code (so the click + captcha attempts happen);
    # the final page no longer shows it: strikes without progress are unknown.
    page = _DeadPage([
        "<html>請輸入下圖驗證碼</html>", "<html>出了點問題</html>",
    ])
    _fake_playwright(monkeypatch, page, error_type=RuntimeError)

    class NeverSolves:
        def solve(self, image_path):
            return None

    submitter = PlaywrightAutoSubmitter(
        timeout_ms=100, sleeper=lambda seconds: None,
        captcha_solver=NeverSolves(), captcha_retries=1,
    )
    result = submitter.submit(OFFICIAL, "A123456789")
    assert (result.ok, result.unknown) == (False, True)


def test_post_click_official_failure_page_stays_a_clean_failure(monkeypatch) -> None:
    # A confirmed 已售完 page means no ticket was accepted: unknown=False,
    # so the task keeps its normal reminder/retry life.
    page = _DeadPage(["<html>此車次已售完，請改選其他時段</html>"])
    _fake_playwright(monkeypatch, page, error_type=RuntimeError)
    submitter = PlaywrightAutoSubmitter(timeout_ms=10, sleeper=lambda seconds: None)
    result = submitter.submit(OFFICIAL, "A123456789")
    assert (result.ok, result.unknown) == (False, False)
    assert "已售完" in result.reason


def test_post_click_browser_error_is_unknown_not_clean_failure(monkeypatch) -> None:
    # AUDIT-1: a submit whose page detaches mid-navigation (the accepted
    # submit navigated away) must never return a clean failure,
    # or the scheduler re-queues it and books a second real ticket.
    page = _DeadPage(["<html>請先選擇乘車日期</html>"])
    page.submit_raises = RuntimeError("Element is not attached to the DOM")
    _fake_playwright(monkeypatch, page, error_type=RuntimeError)
    submitter = PlaywrightAutoSubmitter(timeout_ms=10, sleeper=lambda seconds: None)
    result = submitter.submit(OFFICIAL, "A123456789")
    assert (result.ok, result.unknown) == (False, True)
    assert "瀏覽器錯誤" in result.reason

    # The scheduler honours that flag: parked for a human, never re-queued.
    db = StubDb({"identity": "A123456789"})
    notifier = RecordingNotifier()
    scheduler = _scheduler_with(db, StubSubmitter(result), notifier)
    scheduler.tick()
    _wait_for_browser_rounds()
    assert db.task.status == "waiting_human"
    assert notifier.results == [("t1", "unknown", None)]
    assert notifier.reminded == []


def test_pre_click_browser_error_stays_a_clean_failure(monkeypatch) -> None:
    # The audit's boundary: errors BEFORE the click was issued stay unknown=False
    # (nothing could have been accepted), so the task keeps its reminder life.
    class Boom(_Clickable):
        def type(self, text, delay=0):
            raise RuntimeError("page navigated away")

    page = _DeadPage(["<html>請先選擇乘車日期</html>"])
    page.identity = Boom()
    _fake_playwright(monkeypatch, page, error_type=RuntimeError)
    submitter = PlaywrightAutoSubmitter(timeout_ms=10, sleeper=lambda seconds: None)
    result = submitter.submit(OFFICIAL, "A123456789")
    assert (result.ok, result.unknown) == (False, False)
    assert "瀏覽器錯誤" in result.reason
