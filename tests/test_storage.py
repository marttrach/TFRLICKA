import sqlite3
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet

from tra_sniper.models import BookingRequest
from tra_sniper.storage import Database


def booking_data() -> dict:
    return {
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


def test_task_payload_is_encrypted_and_due_task_is_promoted(tmp_path) -> None:
    database = Database(tmp_path / "app.db", encryption_key=Fernet.generate_key().decode())
    user = database.create_user("user@example.com", "password-hash")
    payload = booking_data()
    scheduled_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    task = database.create_task(
        user.id,
        BookingRequest.from_dict(payload),
        scheduled_at,
        payload,
    )

    assert database.get_task_payload(task.id, user.id)["identity"] == "TEST-ID"
    assert "TEST-ID" not in (tmp_path / "app.db").read_bytes().decode("latin1")
    claimed = database.claim_due_checks((datetime.now(UTC) + timedelta(minutes=6)).isoformat())
    assert [item.id for item in claimed] == [task.id]
    assert database.get_task(task.id, user.id).status == "monitoring"


def test_existing_database_is_migrated_without_losing_users(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO users VALUES (1, 'legacy@example.com', 'existing-hash', ?)",
            (datetime.now(UTC).isoformat(),),
        )

    database = Database(path, encryption_key=Fernet.generate_key().decode())
    user = database.get_user(1)
    assert user is not None
    assert user.email == "legacy@example.com"
    assert user.token_version == 0


def test_login_attempts_and_token_revocation_are_persistent(tmp_path) -> None:
    database = Database(tmp_path / "security.db", encryption_key=Fernet.generate_key().decode())
    user = database.create_user("user@example.com", "password-hash")
    now = datetime.now(UTC)
    for offset in range(5):
        database.record_login_attempt(
            user.email,
            succeeded=False,
            attempted_at=now - timedelta(minutes=offset),
        )
    assert database.is_login_locked(user.email, now=now)

    database.record_login_attempt(user.email, succeeded=True, attempted_at=now)
    assert not database.is_login_locked(user.email, now=now)
    assert database.revoke_user_tokens(user.id)
    assert database.get_user(user.id).token_version == 1


def test_member_profile_is_encrypted_and_can_be_deleted(tmp_path) -> None:
    path = tmp_path / "profile.db"
    database = Database(path, encryption_key=Fernet.generate_key().decode())
    user = database.create_user("profile@example.com", "password-hash")

    saved = database.save_member_profile(
        user.id,
        identity="A123456789",
        member_account="A123456789",
        member_password="railway-password",
    )

    assert saved.identity == "A123456789"
    assert saved.member_password == "railway-password"
    raw_database = path.read_bytes().decode("latin1")
    assert "A123456789" not in raw_database
    assert "railway-password" not in raw_database
    assert database.delete_member_profile(user.id)
    assert database.get_member_profile(user.id) is None
