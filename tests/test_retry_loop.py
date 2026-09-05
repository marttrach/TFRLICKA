"""The task keeps re-preparing until it is booked, cancelled, or the window ends.

A session that ends without a booking code means the person did not finish this
round; the task goes back in the poll loop instead of dying. Nothing here
submits the form or touches the official verification.
"""

import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.browser_session import BookingSessionManager
from tra_sniper.storage import Database

RIDE_DATE = (datetime.now().astimezone().date() + timedelta(days=1)).strftime("%Y/%m/%d")
BOOKING = {
    "identity": "A123456789",
    "start_station": "1180-竹北",
    "end_station": "2200-大甲",
    "outbound": {"ride_date": RIDE_DATE, "train_numbers": ["123"]},
}


class FinishingAutomator:
    """Returns one outcome immediately, so on_finish runs without a browser."""

    def __init__(self, status, booking_code=None) -> None:
        self.status = status
        self.booking_code = booking_code

    def run(self, request, **kwargs):
        on_ready = kwargs.get("on_ready")
        if on_ready:
            on_ready()
        return _Result(self.status, self.booking_code)


class _Result:
    def __init__(self, status, booking_code) -> None:
        self.status = status
        self.booking_code = booking_code
        self.message = status


def _app(tmp_path, automator):
    database = Database(tmp_path / "api.db", encryption_key=Fernet.generate_key().decode())
    app = create_app(
        database,
        TokenManager("t" * 32),
        start_scheduler=False,
        automator_factory=lambda: automator,
    )
    return database, app


def _register(client):
    response = client.post(
        "/auth/register", json={"email": "user@example.com", "password": "very-secure-password"}
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _run_one_session(tmp_path, status, booking_code=None):
    database, app = _app(tmp_path, FinishingAutomator(status, booking_code))
    with TestClient(app) as client:
        headers = _register(client)
        body = {
            "scheduled_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            "booking": BOOKING,
            "poll_interval_seconds": 300,
        }
        body["monitor_until"] = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        created = client.post("/tasks", headers=headers, json=body)
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

        started = client.post(f"/tasks/{task_id}/booking-session", headers=headers)
        assert started.status_code == 201, started.text
        _wait_for_release(app.state.booking_sessions)
        return database, database.get_task(task_id, 1)


def _wait_for_release(sessions, timeout=5.0):
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        if sessions.active is None:
            return
        threading.Event().wait(0.02)
    raise AssertionError("the worker never released the session")


def test_a_timed_out_round_goes_back_in_the_poll_loop(tmp_path) -> None:
    database, task = _run_one_session(tmp_path, "timeout")
    del database
    assert task.status == "monitoring"
    assert task.next_check_at is not None
    delay = datetime.fromisoformat(task.next_check_at) - datetime.now(UTC)
    # Next attempt is one poll interval away, not immediately.
    assert timedelta(minutes=3) < delay <= timedelta(minutes=5)


def test_a_failed_round_goes_back_in_the_poll_loop(tmp_path) -> None:
    _, task = _run_one_session(tmp_path, "failed")
    assert task.status == "monitoring"


def test_a_booking_code_stops_the_loop(tmp_path) -> None:
    _, task = _run_one_session(tmp_path, "completed", booking_code="1234567890")
    assert task.status == "completed"
    assert task.next_check_at is None


def test_cancelling_stops_the_loop(tmp_path) -> None:
    # Cancel is the person saying stop; re-preparing would ignore them.
    _, task = _run_one_session(tmp_path, "cancelled")
    assert task.status == "cancelled"
    assert task.next_check_at is None


def test_the_loop_stops_once_the_monitor_window_closed(tmp_path) -> None:
    # A window that shuts mid-round: the attempt still ends, but nothing requeues.
    database, app = _app(tmp_path, FinishingAutomator("timeout"))
    with TestClient(app) as client:
        headers = _register(client)
        created = client.post(
            "/tasks",
            headers=headers,
            json={
                "scheduled_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                "monitor_until": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                "booking": BOOKING,
                "poll_interval_seconds": 300,
            },
        )
        task_id = created.json()["id"]

    assert database.pause_monitoring(task_id, 1, "waiting_human")
    with database.connect() as connection:
        connection.execute(
            "UPDATE tasks SET monitor_until = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), task_id),
        )

    assert database.resume_monitoring(task_id, 1, 300) is False
    assert database.get_task(task_id, 1).status == "waiting_human"


def test_reap_recovers_browser_before_releasing_stuck_slot(tmp_path) -> None:
    # Without this the single browser slot is stuck until the API restarts.
    del tmp_path
    sessions = BookingSessionManager(ttl_seconds=0)
    session = sessions.acquire("task-1", 1)

    assert sessions.reap() is session  # signals the worker
    assert sessions.active is session  # still waiting for cleanup
    assert sessions.reap() is None  # no second signal while in grace

    session.stopped_at = datetime.now(UTC) - timedelta(hours=1)
    assert sessions.reap() is None  # Without real cleanup the slot stays closed.
    session.recover = lambda: sessions.release(session.token)
    assert sessions.reap() is session
    assert sessions.active is None
    sessions.acquire("task-2", 1)


def test_manual_stop_also_enters_recovery_before_expiry():
    sessions = BookingSessionManager()
    session = sessions.acquire("stuck", 1)
    session.request_stop()
    assert session.stopped_at is not None
    assert not session.is_expired()
    session.stopped_at -= timedelta(seconds=61)
    session.recover = Mock(side_effect=RuntimeError("browser unreachable"))
    sessions.reap()
    assert sessions.active is session  # Cleanup failure must never unlock the desktop.
    session.recover = lambda: sessions.release(session.token)
    sessions.reap()
    assert sessions.active is None


@pytest.mark.parametrize("outcome", ["failed", "timeout"])
def test_cancellation_wins_against_late_failure(tmp_path, monkeypatch, outcome):
    database, app = _app(tmp_path, FinishingAutomator(outcome))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    notifier = Mock(enabled=True)
    notifier.notify_result.side_effect = lambda *args: finished.set()
    app.state.scheduler.notifier = notifier
    original = database.update_task_status

    def update(task_id, user_id, status, **kwargs):
        if status == outcome:
            entered.set()
            assert release.wait(3)
        return original(task_id, user_id, status, **kwargs)

    monkeypatch.setattr(database, "update_task_status", update)
    try:
        with TestClient(app) as client:
            headers = _register(client)
            task_id = client.post('/tasks', headers=headers, json={'booking': BOOKING}).json()['id']
            assert client.post(f'/tasks/{task_id}/booking-session', headers=headers).status_code == 201
            assert entered.wait(2)
            assert client.post(f'/tasks/{task_id}/cancel', headers=headers).status_code == 204
            release.set()
            assert finished.wait(2)
            assert database.get_task(task_id, 1).status == 'cancelled'
            assert database.claim_due_checks() == []
            assert notifier.notify_result.call_args.args[1] == 'cancelled'
    finally:
        release.set()


def test_old_session_result_does_not_follow_a_retry(tmp_path):
    database, app = _app(tmp_path, FinishingAutomator('failed'))
    finished = threading.Event()
    app.state.scheduler.notifier = Mock(enabled=True)
    app.state.scheduler.notifier.notify_result.side_effect = lambda *args: finished.set()
    with TestClient(app) as client:
        headers = _register(client)
        task_id = client.post('/tasks', headers=headers, json={'booking': BOOKING}).json()['id']
        started = client.post(f'/tasks/{task_id}/booking-session', headers=headers).json()
        old_token = started['session_url'].split('/')[2]
        assert finished.wait(2)
        assert database.get_task(task_id, 1).status == 'monitoring'
        url = f'/tasks/{task_id}/booking-result'
        session_headers = {**headers, 'X-Booking-Session': old_token}
        assert client.get(url, headers=session_headers).json()['status'] == 'ended'
        newer = app.state.booking_sessions.acquire(task_id, 1)
        newer.status = 'waiting_verification'
        assert client.get(url, headers=session_headers).json()['status'] == 'ended'
        assert client.get(f'/tasks/{task_id}/booking-result', headers=headers).json()['status'] == 'waiting_verification'
        app.state.booking_sessions.release(newer.token)


@pytest.mark.parametrize('late_code', [None, '1234567890'])
def test_stuck_worker_recovery_finishes_once_and_preserves_cancel(tmp_path, late_code):
    release, ready, reset_called = threading.Event(), threading.Event(), threading.Event()

    class Stuck:
        def run(self, request, **kwargs):
            kwargs['on_ready']()
            ready.set()
            assert release.wait(5)
            return _Result('completed' if late_code else 'failed', late_code)

        def reset_browser(self):
            reset_called.set()

    database, app = _app(tmp_path, Stuck())
    app.state.scheduler.notifier = Mock(enabled=True)
    try:
        with TestClient(app) as client:
            headers = _register(client)
            task_id = client.post('/tasks', headers=headers, json={'booking': BOOKING}).json()['id']
            started = client.post(f'/tasks/{task_id}/booking-session', headers=headers).json()
            assert ready.wait(1)
            assert client.delete(started['session_url'].rstrip('/'), headers=headers).status_code == 204
            sessions = app.state.booking_sessions
            session = sessions.active
            session.stopped_at -= timedelta(seconds=61)
            sessions.reap()
            assert reset_called.is_set()
            assert sessions.active is None
            assert database.get_task(task_id, 1).status == 'cancelled'
            release.set()
            for worker in threading.enumerate():
                if worker.name == f'booking-session-{task_id}':
                    worker.join(2)
                    assert not worker.is_alive()
            final = database.get_task(task_id, 1)
            assert final.status == ('completed' if late_code else 'cancelled')
            assert final.booking_code == late_code
            assert app.state.scheduler.notifier.notify_result.call_count == (2 if late_code else 1)
    finally:
        release.set()
