"""Auto-submit mode (reCAPTCHA v3 via headless browser).

The browser itself is never launched here: parse_booking_outcome is pure and
the scheduler runs against a stub submitter, so the suite stays fast and does
not require the optional playwright extra.
"""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.autobook import SubmitResult, parse_booking_outcome
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
