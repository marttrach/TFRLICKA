import threading
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

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
