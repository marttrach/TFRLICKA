from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.storage import Database


def test_member_and_task_flow(tmp_path) -> None:
    database = Database(tmp_path / "api.db", encryption_key=Fernet.generate_key().decode())
    app = create_app(database, TokenManager("t" * 32), start_scheduler=False)
    booking = {
        "identity": "TEST-ID",
        "start_station": "1000-臺北",
        "end_station": "3300-臺中",
        "outbound": {
            "ride_date": (datetime.now().astimezone().date() + timedelta(days=1)).strftime(
                "%Y/%m/%d"
            ),
            "train_numbers": ["110"],
        },
    }

    with TestClient(app) as client:
        registered = client.post(
            "/auth/register",
            json={"email": "user@example.com", "password": "very-secure-password"},
        )
        assert registered.status_code == 201
        token = registered.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        created = client.post(
            "/tasks",
            headers=headers,
            json={
                "scheduled_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "booking": booking,
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert "identity" not in created.text

        tasks = client.get("/tasks", headers=headers)
        assert tasks.status_code == 200
        assert tasks.json()[0]["route"] == "1000-臺北 → 3300-臺中"

        config = client.get(f"/tasks/{task_id}/config", headers=headers)
        assert config.json()["identity"] == "TEST-ID"

        cancelled = client.post(f"/tasks/{task_id}/cancel", headers=headers)
        assert cancelled.status_code == 204
