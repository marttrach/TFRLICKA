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


def test_terminal_states_resist_late_callbacks_but_keep_a_real_booking_code(tmp_path):
    database = Database(tmp_path / "states.db", encryption_key=Fernet.generate_key().decode())
    user = database.create_user("audit@example.com", "hash")
    payload = booking_data()
    task = database.create_task(user.id, BookingRequest.from_dict(payload),
                                datetime.now(UTC).isoformat(), payload)
    assert database.update_task_status(task.id, user.id, "cancelled")
    assert not database.update_task_status(task.id, user.id, "failed")
    assert not database.resume_monitoring(task.id, user.id)
    # Cancellation must never discard a code returned by an already-submitted booking.
    assert database.update_task_status(task.id, user.id, "completed", booking_code="1234567890")
    assert not database.update_task_status(task.id, user.id, "cancelled")
    assert not database.resume_monitoring(task.id, user.id)
    assert database.get_task(task.id, user.id).booking_code == "1234567890"


def test_scheduled_completion_cannot_resurrect_a_cancelled_task(tmp_path) -> None:
    database = Database(tmp_path / "only-from.db", encryption_key=Fernet.generate_key().decode())
    user = database.create_user("guard@example.com", "password-hash")
    payload = booking_data()
    task = database.create_task(
        user.id, BookingRequest.from_dict(payload),
        (datetime.now(UTC) + timedelta(minutes=5)).isoformat(), payload,
    )
    assert database.update_task_status(task.id, user.id, "cancelled")

    # The scheduler's automated completed write is refused by the guard...
    assert not database.update_task_status(
        task.id, user.id, "completed", booking_code="AB12345678",
        only_from=("autobooking", "monitoring", "scheduled"),
    )
    assert database.get_task(task.id, user.id).status == "cancelled"
    # ...while the person's own report_booked path keeps its carve-out.
    assert database.update_task_status(task.id, user.id, "completed", booking_code="AB12345678")
    assert database.get_task(task.id, user.id).booking_code == "AB12345678"


def test_park_for_human_review_leaves_the_poll_loop(tmp_path) -> None:
    database = Database(tmp_path / "park.db", encryption_key=Fernet.generate_key().decode())
    user = database.create_user("park@example.com", "password-hash")
    payload = booking_data()
    task = database.create_task(
        user.id, BookingRequest.from_dict(payload),
        (datetime.now(UTC) - timedelta(minutes=5)).isoformat(), payload,
    )
    assert database.park_for_human_review(task.id, user.id, last_error="結果未知")
    parked = database.get_task(task.id, user.id)
    assert parked.status == "waiting_human"
    assert parked.next_check_at is None
    assert parked.last_error == "結果未知"
    assert database.claim_due_checks((datetime.now(UTC) + timedelta(hours=1)).isoformat()) == []
    assert database.list_tasks_by_status("waiting_human")[0].id == task.id


def test_check_failure_counter_round_trip(tmp_path) -> None:
    database = Database(tmp_path / "strikes.db", encryption_key=Fernet.generate_key().decode())
    user = database.create_user("strike@example.com", "password-hash")
    payload = booking_data()
    task = database.create_task(
        user.id, BookingRequest.from_dict(payload),
        (datetime.now(UTC) + timedelta(minutes=5)).isoformat(), payload,
    )
    assert database.record_check_failure(task.id) == 1
    assert database.record_check_failure(task.id) == 2
    database.clear_check_failures(task.id)
    assert database.record_check_failure(task.id) == 1


def test_database_runs_in_wal_mode(tmp_path) -> None:
    database = Database(tmp_path / "wal.db", encryption_key=Fernet.generate_key().decode())
    with database.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_reminder_only_modes_migrate_to_auto_submit_on_start(tmp_path) -> None:
    # monitor_only / book_when_available were removed; rows left by an older
    # build must come back as auto_submit, not as a mode nothing handles.
    key = Fernet.generate_key().decode()
    database = Database(tmp_path / "app.db", encryption_key=key)
    user = database.create_user("user@example.com", "password-hash")
    tasks = [
        database.create_task(
            user.id, BookingRequest.from_dict(booking_data()),
            datetime.now(UTC).isoformat(), booking_data(),
        )
        for _ in range(2)
    ]
    with database.connect() as connection:
        for task, old_mode in zip(tasks, ("monitor_only", "book_when_available"), strict=True):
            connection.execute("UPDATE tasks SET mode = ? WHERE id = ?", (old_mode, task.id))

    reopened = Database(tmp_path / "app.db", encryption_key=key)
    assert {reopened.get_task(task.id, user.id).mode for task in tasks} == {"auto_submit"}
