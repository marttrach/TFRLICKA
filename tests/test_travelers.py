from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.storage import Database


def _db(tmp_path, key):
    return Database(tmp_path / "api.db", encryption_key=key)


def _register(client, email="user@example.com"):
    response = client.post(
        "/auth/register", json={"email": email, "password": "very-secure-password"}
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_multiple_travelers_are_kept_separately(tmp_path) -> None:
    database = _db(tmp_path, Fernet.generate_key().decode())
    app = create_app(database, TokenManager("t" * 32), start_scheduler=False)
    with TestClient(app) as client:
        headers = _register(client)

        for label, identity in (("我自己", "A123456789"), ("老婆", "B234567890")):
            created = client.post(
                "/travelers", headers=headers, json={"label": label, "identity": identity}
            )
            assert created.status_code == 201

        listed = client.get("/travelers", headers=headers).json()
        assert {item["label"] for item in listed} == {"我自己", "老婆"}
        assert {item["identity"] for item in listed} == {"A123456789", "B234567890"}


def test_duplicate_label_is_refused(tmp_path) -> None:
    database = _db(tmp_path, Fernet.generate_key().decode())
    app = create_app(database, TokenManager("t" * 32), start_scheduler=False)
    with TestClient(app) as client:
        headers = _register(client)
        body = {"label": "我自己", "identity": "A123456789"}
        assert client.post("/travelers", headers=headers, json=body).status_code == 201
        assert client.post("/travelers", headers=headers, json=body).status_code == 409


def test_traveler_is_scoped_to_its_owner(tmp_path) -> None:
    database = _db(tmp_path, Fernet.generate_key().decode())
    app = create_app(database, TokenManager("t" * 32), start_scheduler=False)
    with TestClient(app) as client:
        owner = _register(client, "owner@example.com")
        created = client.post(
            "/travelers", headers=owner, json={"label": "我自己", "identity": "A123456789"}
        ).json()

        intruder = _register(client, "intruder@example.com")
        assert client.get("/travelers", headers=intruder).json() == []
        assert client.delete(
            f"/travelers/{created['id']}", headers=intruder
        ).status_code == 404
        assert client.put(
            f"/travelers/{created['id']}",
            headers=intruder,
            json={"label": "偷改", "identity": "C000000000"},
        ).status_code == 404


def test_task_uses_the_selected_traveler_identity(tmp_path) -> None:
    database = _db(tmp_path, Fernet.generate_key().decode())
    app = create_app(database, TokenManager("t" * 32), start_scheduler=False)
    ride_date = (datetime.now().astimezone().date() + timedelta(days=1)).strftime("%Y/%m/%d")
    with TestClient(app) as client:
        headers = _register(client)
        wife = client.post(
            "/travelers", headers=headers, json={"label": "老婆", "identity": "B234567890"}
        ).json()

        created = client.post(
            "/tasks",
            headers=headers,
            json={
                "scheduled_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "traveler_id": wife["id"],
                "booking": {
                    "start_station": "1000-臺北",
                    "end_station": "3300-臺中",
                    "outbound": {"ride_date": ride_date, "train_numbers": ["110"]},
                },
            },
        )
        assert created.status_code == 201
        # The identity is never echoed back in the task response.
        assert "B234567890" not in created.text

        task_id = created.json()["id"]
        config = client.get(f"/tasks/{task_id}/config", headers=headers).json()
        assert config["identity"] == "B234567890"


def test_unknown_traveler_is_rejected(tmp_path) -> None:
    database = _db(tmp_path, Fernet.generate_key().decode())
    app = create_app(database, TokenManager("t" * 32), start_scheduler=False)
    ride_date = (datetime.now().astimezone().date() + timedelta(days=1)).strftime("%Y/%m/%d")
    with TestClient(app) as client:
        headers = _register(client)
        response = client.post(
            "/tasks",
            headers=headers,
            json={
                "scheduled_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "traveler_id": 999,
                "booking": {
                    "start_station": "1000-臺北",
                    "end_station": "3300-臺中",
                    "outbound": {"ride_date": ride_date, "train_numbers": ["110"]},
                },
            },
        )
        assert response.status_code == 404


def test_existing_member_profile_identity_survives_the_upgrade(tmp_path) -> None:
    """A database written before `travelers` existed must not lose its identity."""
    key = Fernet.generate_key().decode()
    path = tmp_path / "api.db"

    database = _db(tmp_path, key)
    user = database.create_user("user@example.com", "hash")
    database.save_member_profile(
        user.id, identity="A123456789", member_account="member", member_password="pw"
    )
    # Simulate the pre-upgrade schema.
    with database.connect() as connection:
        connection.execute("DROP TABLE travelers")

    reopened = Database(path, encryption_key=key)
    travelers = reopened.list_travelers(user.id)
    assert [(t.label, t.identity) for t in travelers] == [("預設", "A123456789")]


def test_seeding_does_not_resurrect_deleted_travelers(tmp_path) -> None:
    key = Fernet.generate_key().decode()
    path = tmp_path / "api.db"

    database = _db(tmp_path, key)
    user = database.create_user("user@example.com", "hash")
    database.save_member_profile(
        user.id, identity="A123456789", member_account="member", member_password="pw"
    )
    with database.connect() as connection:
        connection.execute("DROP TABLE travelers")

    reopened = Database(path, encryption_key=key)
    seeded = reopened.list_travelers(user.id)
    assert len(seeded) == 1
    assert reopened.delete_traveler(seeded[0].id, user.id) is True

    # The table already exists now, so restarting must leave the deletion alone.
    restarted = Database(path, encryption_key=key)
    assert restarted.list_travelers(user.id) == []


def test_traveler_identity_is_encrypted_at_rest(tmp_path) -> None:
    database = _db(tmp_path, Fernet.generate_key().decode())
    user = database.create_user("user@example.com", "hash")
    database.create_traveler(user.id, label="我自己", identity="A123456789")

    raw = (tmp_path / "api.db").read_bytes()
    assert b"A123456789" not in raw


def test_update_traveler_round_trips(tmp_path) -> None:
    database = _db(tmp_path, Fernet.generate_key().decode())
    user = database.create_user("user@example.com", "hash")
    created = database.create_traveler(user.id, label="我自己", identity="A123456789")

    updated = database.update_traveler(
        created.id, user.id, label="本人", identity="B234567890"
    )
    assert updated is not None
    assert (updated.label, updated.identity) == ("本人", "B234567890")

    assert database.update_traveler(
        created.id, user.id + 1, label="x", identity="C000000000"
    ) is None


def test_duplicate_label_raises_at_storage_layer(tmp_path) -> None:
    database = _db(tmp_path, Fernet.generate_key().decode())
    user = database.create_user("user@example.com", "hash")
    database.create_traveler(user.id, label="我自己", identity="A123456789")
    with pytest.raises(ValueError, match="同名"):
        database.create_traveler(user.id, label="我自己", identity="B234567890")
