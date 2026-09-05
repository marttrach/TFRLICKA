from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.storage import Database

TOMORROW = (datetime.now().astimezone().date() + timedelta(days=1)).strftime("%Y/%m/%d")
BOOKING = {
    "identity": "TEST-ID",
    "start_station": "1180-竹北",
    "end_station": "2200-大甲",
    "outbound": {"ride_date": TOMORROW, "train_numbers": ["123"]},
}


def _app(tmp_path):
    database = Database(tmp_path / "api.db", encryption_key=Fernet.generate_key().decode())
    return database, create_app(database, TokenManager("t" * 32), start_scheduler=False)


def _register(client, email="user@example.com"):
    response = client.post(
        "/auth/register", json={"email": email, "password": "very-secure-password"}
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create(client, headers, **extra):
    body = {
        "scheduled_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "booking": BOOKING,
        **extra,
    }
    response = client.post("/tasks", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_delete_removes_the_task_from_the_queue(tmp_path) -> None:
    _, app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _register(client)
        task_id = _create(client, headers)["id"]

        assert client.delete(f"/tasks/{task_id}", headers=headers).status_code == 204
        assert client.get("/tasks", headers=headers).json() == []


def test_delete_cannot_reach_another_users_task(tmp_path) -> None:
    _, app = _app(tmp_path)
    with TestClient(app) as client:
        owner = _register(client, "owner@example.com")
        task_id = _create(client, owner)["id"]

        intruder = _register(client, "intruder@example.com")
        assert client.delete(f"/tasks/{task_id}", headers=intruder).status_code == 404
        # Still there for its owner.
        assert len(client.get("/tasks", headers=owner).json()) == 1


def test_delete_is_refused_while_a_booking_session_holds_the_task(tmp_path) -> None:
    _, app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _register(client)
        task_id = _create(client, headers)["id"]
        app.state.booking_sessions.acquire(task_id, 1)

        response = client.delete(f"/tasks/{task_id}", headers=headers)
        assert response.status_code == 409
        assert len(client.get("/tasks", headers=headers).json()) == 1


def test_deleting_a_missing_task_is_404(tmp_path) -> None:
    _, app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _register(client)
        assert client.delete("/tasks/does-not-exist", headers=headers).status_code == 404


def test_chosen_train_is_stored_and_shown_on_the_task(tmp_path) -> None:
    _, app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _register(client)
        created = _create(client, headers, train_label="自強(3000) 123 · 08:30 → 11:45")
        assert created["train_label"] == "自強(3000) 123 · 08:30 → 11:45"
        assert client.get("/tasks", headers=headers).json()[0]["train_label"] == (
            "自強(3000) 123 · 08:30 → 11:45"
        )


def test_a_monitoring_task_can_still_be_cancelled(tmp_path) -> None:
    # The dashboard offers 停止並取消任務 for monitoring tasks, and the patrol
    # loop makes monitoring the usual state, so refusing it stranded the task.
    database, app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _register(client)
        task_id = _create(client, headers)["id"]
        database.update_task_status(task_id, 1, "monitoring")

        assert client.post(f"/tasks/{task_id}/cancel", headers=headers).status_code == 204
