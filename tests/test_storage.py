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
    assert database.promote_due_tasks((datetime.now(UTC) + timedelta(minutes=6)).isoformat()) == 1
    assert database.get_task(task.id, user.id).status == "waiting_human"
