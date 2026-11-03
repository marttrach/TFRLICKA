"""Auto-submit mode (reCAPTCHA v3 via headless browser).

The browser itself is never launched here: parse_booking_outcome is pure and
the scheduler runs against a stub submitter, so the suite stays fast and does
not require the optional playwright extra. The picture-code fallback (the
official response when v3 scores a datacenter IP zero) is covered with a fake
OCR chain and a duck-typed fake page.
"""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.autobook import (
    OcrChain,
    PlaywrightAutoSubmitter,
    SubmitResult,
    page_shows_captcha,
    parse_booking_outcome,
)
from tra_sniper.notifications import WebhookNotifier
from tra_sniper.scheduler import TaskScheduler
from tra_sniper.storage import (
    MODE_AUTO_SUBMIT,
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
    """The scheduler surface _try_autobook touches, with an assertion journal."""

    def __init__(self, payload: dict) -> None:
        self.task = SimpleNamespace(id="t1", user_id=1, status="monitoring", mode=MODE_AUTO_SUBMIT)
        self.payload = payload
        self.statuses: list[tuple[str, str | None]] = []

    def expire_finished_monitors(self):
        return []

    def claim_due_checks(self):
        return [self.task]

    def get_task_payload(self, task_id, user_id):
        return self.payload

    def get_task(self, task_id, user_id):
        return self.task

    def update_task_status(self, task_id, user_id, status, *, last_error=None, booking_code=None):
        self.statuses.append((status, booking_code))
        self.task = SimpleNamespace(
            id=task_id, user_id=user_id, status=status, mode=self.task.mode,
            booking_code=booking_code,
        )
        return True

    def pause_monitoring(self, task_id, user_id, status):
        self.statuses.append((status, None))
        return True


class StubSubmitter:
    def __init__(self, result: SubmitResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def submit(self, url, identity):
        self.calls.append((url, identity))
        return self.result


def _scheduler(db, submitter, notifier=None) -> TaskScheduler:
    notifier = notifier or WebhookNotifier()
    notifier.official_url_for = lambda task: OFFICIAL
    return TaskScheduler(db, notifier=notifier, autobook=submitter)  # type: ignore[arg-type]


def test_auto_submit_books_completes_task_without_reminder() -> None:
    db = StubDb({"identity": "A123456789"})
    submitter = StubSubmitter(SubmitResult(True, booking_code="A1234567"))
    scheduler = _scheduler(db, submitter)
    assert scheduler.tick() == 0  # booked; the person does not need a reminder
    assert submitter.calls == [(OFFICIAL, "A123456789")]
    assert ("completed", "A1234567") in db.statuses


def test_auto_submit_failure_falls_back_to_reminder() -> None:
    db = StubDb({"identity": "A123456789"})
    submitter = StubSubmitter(SubmitResult(False, reason="已售完"))
    scheduler = _scheduler(db, submitter)
    assert scheduler.tick() == 1  # the reminder covers for the browser
    assert not any(status == "completed" for status, _ in db.statuses)


def test_auto_submit_without_identity_reminds_and_explains() -> None:
    db = StubDb({"identity": ""})
    submitter = StubSubmitter(SubmitResult(True))
    scheduler = _scheduler(db, submitter)
    assert scheduler.tick() == 1
    assert submitter.calls == []  # never sends an empty ID to the page


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

    app = create_app(database=db, token_manager=TokenManager(), tdx_client=FakeTdx(),
                     start_scheduler=False, autobook=StubSubmitter(SubmitResult(True, booking_code="A1234567")))
    with TestClient(app) as client:
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


# ---- picture-code fallback ---------------------------------------------------


class FakeChain:
    def __init__(self, code: str | None) -> None:
        self.code = code
        self.calls = 0

    def solve(self, image_path):
        self.calls += 1
        return self.code


class FakeElement:
    def __init__(self, src=None, visible=True) -> None:
        self.src = src
        self._visible = visible
        self.filled = None
        self.clicked = False
        self.evaluated = None

    def is_visible(self):
        return self._visible

    def get_attribute(self, name):
        return self.src

    def fill(self, value):
        self.filled = value

    def click(self):
        self.clicked = True

    def evaluate(self, script):
        self.evaluated = script


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


def test_page_shows_captcha_marker() -> None:
    assert page_shows_captcha("請輸入下圖驗證碼")
    assert not page_shows_captcha("訂票成功")


def test_solve_captcha_reads_image_fills_and_resubmits() -> None:
    page = FakePage()
    submitter = PlaywrightAutoSubmitter(
        captcha_solver=FakeChain("AB3D9F"), sleeper=lambda seconds: None
    )
    assert submitter._solve_captcha(page) is True
    # the picture is fetched with the page's own (session-bearing) request
    assert page.request.urls and "/tra-tip-web/tip/player/picture" in page.request.urls[0]
    assert page.verify.filled == "AB3D9F"
    assert page.verify_type.evaluated is not None  # verifyType set to 'text'
    assert page.button.clicked is True


def test_solve_captcha_without_readable_code_gives_up_quietly() -> None:
    page = FakePage()
    submitter = PlaywrightAutoSubmitter(
        captcha_solver=FakeChain(None), sleeper=lambda seconds: None
    )
    assert submitter._solve_captcha(page) is False
    assert page.verify.filled is None
