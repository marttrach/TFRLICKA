import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper import storage
from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.storage import Database


class HeldAutomator:
    """Blocks inside run() so the session stays active while a test inspects it."""

    def __init__(self, release: threading.Event) -> None:
        self.release = release

    def run(self, request, **kwargs):
        stop = kwargs.get("stop_event")
        while not self.release.wait(timeout=0.02):
            if stop is not None and stop.is_set():
                break
        return _Result("cancelled", None, "released")


class _Result:
    def __init__(self, status, booking_code, message) -> None:
        self.status = status
        self.booking_code = booking_code
        self.message = message


def _booking():
    ride_date = (datetime.now().astimezone().date() + timedelta(days=1)).strftime("%Y/%m/%d")
    return {
        "identity": "A123456789",
        "start_station": "1000-臺北",
        "end_station": "3300-臺中",
        "outbound": {"ride_date": ride_date, "train_numbers": ["110"]},
    }


def _app(tmp_path, release: threading.Event):
    database = Database(tmp_path / "api.db", encryption_key=Fernet.generate_key().decode())
    app = create_app(
        database,
        TokenManager("t" * 32),
        start_scheduler=False,
        automator_factory=lambda: HeldAutomator(release),
    )
    return database, app


def _register(client, email="user@example.com"):
    response = client.post(
        "/auth/register", json={"email": email, "password": "very-secure-password"}
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_task(client, headers):
    created = client.post(
        "/tasks",
        headers=headers,
        json={
            "scheduled_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "booking": _booking(),
        },
    )
    assert created.status_code == 201
    return created.json()["id"]


def test_booking_session_starts_and_guards_the_stream(tmp_path) -> None:
    release = threading.Event()
    _, app = _app(tmp_path, release)
    try:
        with TestClient(app) as client:
            headers = _register(client)
            task_id = _create_task(client, headers)

            started = client.post(f"/tasks/{task_id}/booking-session", headers=headers)
            assert started.status_code == 201
            body = started.json()
            token = body["session_url"].strip("/").split("/")[-1]
            # The dashboard slices the token out of this path to cancel the
            # session, so the shape is part of the contract, not a detail.
            assert body["session_url"] == f"/booking-session/{token}/"

            # The response must tell the user they still solve and submit.
            assert "不會辨識驗證碼" in body["notice"]

            assert client.get(f"/booking-session/{token}/verify").status_code == 204
            assert client.get("/booking-session/not-a-real-token/verify").status_code == 403
    finally:
        release.set()


def test_second_concurrent_session_is_refused(tmp_path) -> None:
    release = threading.Event()
    _, app = _app(tmp_path, release)
    try:
        with TestClient(app) as client:
            headers = _register(client)
            first_task = _create_task(client, headers)
            second_task = _create_task(client, headers)

            assert client.post(
                f"/tasks/{first_task}/booking-session", headers=headers
            ).status_code == 201

            busy = client.post(f"/tasks/{second_task}/booking-session", headers=headers)
            assert busy.status_code == 409
            assert "Retry-After" in busy.headers
            assert first_task in busy.json()["detail"]
    finally:
        release.set()


def test_other_user_cannot_open_session_for_foreign_task(tmp_path) -> None:
    release = threading.Event()
    _, app = _app(tmp_path, release)
    try:
        with TestClient(app) as client:
            owner = _register(client, "owner@example.com")
            task_id = _create_task(client, owner)

            intruder = _register(client, "intruder@example.com")
            response = client.post(f"/tasks/{task_id}/booking-session", headers=intruder)
            assert response.status_code == 404
    finally:
        release.set()


def test_cancelling_session_releases_the_lock(tmp_path) -> None:
    release = threading.Event()
    _, app = _app(tmp_path, release)
    finished = threading.Event()
    app.state.scheduler.notifier = Mock(enabled=True)
    app.state.scheduler.notifier.notify_result.side_effect = lambda *args: finished.set()
    try:
        with TestClient(app) as client:
            headers = _register(client)
            first_task = _create_task(client, headers)
            second_task = _create_task(client, headers)

            started = client.post(f"/tasks/{first_task}/booking-session", headers=headers)
            token = started.json()["session_url"].strip("/").split("/")[-1]

            assert client.delete(f"/booking-session/{token}", headers=headers).status_code == 204
            # A released token stops resolving, and the lock is free again.
            assert client.get(f"/booking-session/{token}/verify").status_code == 403
            assert finished.wait(1), "the worker must close its browser before the slot is reused"
            assert client.post(
                f"/tasks/{second_task}/booking-session", headers=headers
            ).status_code == 201
    finally:
        release.set()


def test_booking_result_falls_back_to_persisted_task(tmp_path) -> None:
    release = threading.Event()
    database, app = _app(tmp_path, release)
    with TestClient(app) as client:
        headers = _register(client)
        task_id = _create_task(client, headers)

        database.update_task_status(
            task_id, 1, "completed", booking_code="1234567890"
        )

        result = client.get(f"/tasks/{task_id}/booking-result", headers=headers)
        assert result.status_code == 200
        assert result.json()["booking_code"] == "1234567890"
        assert result.json()["status"] == "completed"


def test_booking_code_survives_a_later_status_update(tmp_path) -> None:
    database = Database(tmp_path / "api.db", encryption_key=Fernet.generate_key().decode())
    app = create_app(database, TokenManager("t" * 32), start_scheduler=False)
    with TestClient(app) as client:
        headers = _register(client)
        task_id = _create_task(client, headers)

        database.update_task_status(task_id, 1, "completed", booking_code="9876543210")
        database.update_task_status(task_id, 1, "completed", last_error=None)

        assert database.get_task(task_id, 1).booking_code == "9876543210"


@pytest.mark.parametrize("monitor", [False, True])
def test_scheduler_prepares_then_notifies_once_and_reuses_the_page(tmp_path, monkeypatch, monitor):
    release = threading.Event()
    entered = threading.Event()
    prepare = threading.Event()
    notified = threading.Event()
    finished = threading.Event()
    database, app = _app(tmp_path, release)
    scheduler = app.state.scheduler
    notifier = Mock(enabled=True)
    notifier.notify.side_effect = lambda *args: notified.set()
    notifier.notify_result.side_effect = lambda *args: finished.set()
    scheduler.notifier = notifier

    def run(self, request, **kwargs):
        entered.set()
        assert prepare.wait(2)
        kwargs["on_ready"]()
        kwargs["on_ready"]()
        assert kwargs["stop_event"].wait(2)
        return _Result("cancelled", None, "cancelled")

    monkeypatch.setattr(HeldAutomator, "run", run)
    try:
        with TestClient(app) as client:
            headers = _register(client)
            until = datetime.now(UTC) + timedelta(hours=1)
            created = client.post("/tasks", headers=headers, json={
                "booking": _booking(),
                "monitor_until": until.isoformat() if monitor else None,
            })
            task_id = created.json()["id"]
            assert scheduler.tick() == 1
            assert entered.wait(1)
            session = app.state.booking_sessions.active
            assert session.status == "preparing"
            notifier.notify.assert_not_called()
            assert database.get_task(task_id, 1).status == "waiting_human"
            assert database.get_task(task_id, 1).next_check_at is None
            if monitor:
                assert session.expires_at <= until

            prepare.set()
            assert notified.wait(1)
            monkeypatch.setattr(storage, "utc_now", lambda: (
                datetime.now(UTC) + timedelta(minutes=6)
            ).isoformat())
            assert scheduler.tick() == 0
            # Database state still pauses the task after a scheduler restart.
            from tra_sniper.scheduler import TaskScheduler
            assert TaskScheduler(database, notifier=notifier).tick() == 0
            notifier.notify.assert_called_once()

            response = client.post(f"/tasks/{task_id}/booking-session", headers=headers)
            assert response.status_code == 201
            assert response.json()["session_url"] == f"/booking-session/{session.token}/"
            assert app.state.booking_sessions.active is session
            notifier.notify.assert_called_once()

            assert client.post(f"/tasks/{task_id}/cancel", headers=headers).status_code == 204
            assert finished.wait(1)
            assert database.get_task(task_id, 1).status == "cancelled"
            assert scheduler.tick() == 0
            assert app.state.booking_sessions.active is None
    finally:
        prepare.set()
        release.set()


def test_monitor_only_reminds_once_without_opening_a_browser(tmp_path, monkeypatch):
    release = threading.Event()
    _, app = _app(tmp_path, release)
    notifier = Mock(enabled=True)
    app.state.scheduler.notifier = notifier
    with TestClient(app) as client:
        headers = _register(client)
        created = client.post("/tasks", headers=headers, json={
            "booking": _booking(), "mode": "monitor_only",
            "monitor_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        })
        assert created.status_code == 201
        assert app.state.scheduler.tick() == 1
        monkeypatch.setattr(storage, "utc_now", lambda: (
            datetime.now(UTC) + timedelta(minutes=6)
        ).isoformat())
        assert app.state.scheduler.tick() == 0
        notifier.notify.assert_called_once()
        assert app.state.booking_sessions.active is None


def test_preparation_failure_stops_without_a_ready_notification(tmp_path, monkeypatch):
    def fail(self, request, **kwargs):
        raise RuntimeError("official form was not recognised")

    monkeypatch.setattr(HeldAutomator, "run", fail)
    database, app = _app(tmp_path, threading.Event())
    finished = threading.Event()
    notifier = Mock(enabled=True)
    notifier.notify_result.side_effect = lambda *args: finished.set()
    app.state.scheduler.notifier = notifier
    with TestClient(app) as client:
        headers = _register(client)
        task_id = client.post("/tasks", headers=headers, json={"booking": _booking()}).json()["id"]
        assert app.state.scheduler.tick() == 1
        assert finished.wait(1)
        assert database.get_task(task_id, 1).status == "failed"
        assert app.state.scheduler.tick() == 0
        notifier.notify.assert_not_called()
        notifier.notify_result.assert_called_once()


def test_expired_worker_keeps_slot_until_cleanup_and_does_not_restart(tmp_path, monkeypatch):
    cleanup = threading.Event()
    finished = threading.Event()
    database, app = _app(tmp_path, threading.Event())
    notifier = Mock(enabled=True)
    notifier.notify_result.side_effect = lambda *args: finished.set()
    app.state.scheduler.notifier = notifier

    def run(self, request, **kwargs):
        kwargs["on_ready"]()
        assert kwargs["stop_event"].wait(2)
        assert cleanup.wait(2)
        return _Result("cancelled", None, "stopped")

    monkeypatch.setattr(HeldAutomator, "run", run)
    try:
        with TestClient(app) as client:
            headers = _register(client)
            task_id = client.post("/tasks", headers=headers, json={"booking": _booking()}).json()["id"]
            assert app.state.scheduler.tick() == 1
            session = app.state.booking_sessions.active
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            assert app.state.scheduler.tick() == 0
            assert session.stop.is_set()
            second = _create_task(client, headers)
            assert client.post(f"/tasks/{second}/booking-session", headers=headers).status_code == 409
            assert client.delete(f"/tasks/{task_id}", headers=headers).status_code == 409
            cleanup.set()
            assert finished.wait(1)
            assert database.get_task(task_id, 1).status == "timeout"
            assert app.state.booking_sessions.active is None
            assert app.state.scheduler.tick() == 0
    finally:
        cleanup.set()
