"""Periodic monitoring: intervals, stop conditions, and no duplicate work.

Every timing assertion drives the clock explicitly. Nothing here sleeps.
"""

from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet

from tra_sniper.models import BookingRequest
from tra_sniper.storage import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    MODE_BOOK_WHEN_AVAILABLE,
    MODE_MONITOR_ONLY,
    Database,
)


def _db(tmp_path):
    return Database(tmp_path / "monitor.db", encryption_key=Fernet.generate_key().decode())


def _booking():
    ride_date = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    return {
        "identity": "A123456789",
        "start_station": "1000-臺北",
        "end_station": "3300-臺中",
        "outbound": {"ride_date": ride_date, "train_numbers": ["109"]},
    }


def _task(database, user_id, *, start, until, mode=MODE_BOOK_WHEN_AVAILABLE, interval=None):
    data = _booking()
    return database.create_task(
        user_id,
        BookingRequest.from_dict(data),
        start.isoformat(),
        data,
        mode=mode,
        poll_interval_seconds=interval or DEFAULT_POLL_INTERVAL_SECONDS,
        monitor_until=until.isoformat() if until else None,
    )


def test_default_interval_is_five_minutes(tmp_path) -> None:
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    now = datetime.now(UTC)
    task = _task(database, user.id, start=now, until=now + timedelta(hours=2))

    assert task.poll_interval_seconds == 300
    assert DEFAULT_POLL_INTERVAL_SECONDS == 300


def test_next_check_is_one_interval_after_the_claim(tmp_path) -> None:
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    now = datetime.now(UTC)
    _task(database, user.id, start=now - timedelta(minutes=1), until=now + timedelta(hours=2))

    moment = now.isoformat()
    claimed = database.claim_due_checks(moment)
    assert len(claimed) == 1

    stored = database.get_task(claimed[0].id, user.id)
    assert stored.last_checked_at == moment
    # A claim schedules the following check; it does not sleep for it.
    gap = datetime.fromisoformat(stored.next_check_at) - datetime.fromisoformat(moment)
    assert gap == timedelta(seconds=300)


def test_a_task_is_never_claimed_twice_at_once(tmp_path) -> None:
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    now = datetime.now(UTC)
    _task(database, user.id, start=now - timedelta(minutes=1), until=now + timedelta(hours=2))

    moment = now.isoformat()
    first = database.claim_due_checks(moment)
    second = database.claim_due_checks(moment)

    assert len(first) == 1
    assert second == [], "the claim must move next_check_at out of reach"


def test_check_runs_again_after_the_interval_elapses(tmp_path) -> None:
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    now = datetime.now(UTC)
    _task(
        database,
        user.id,
        start=now - timedelta(minutes=1),
        until=now + timedelta(hours=2),
        mode=MODE_MONITOR_ONLY,
    )

    assert len(database.claim_due_checks(now.isoformat())) == 1
    assert database.claim_due_checks((now + timedelta(seconds=299)).isoformat()) == []
    assert len(database.claim_due_checks((now + timedelta(seconds=301)).isoformat())) == 1


def test_monitoring_stops_at_the_deadline(tmp_path) -> None:
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    now = datetime.now(UTC)
    task = _task(
        database,
        user.id,
        start=now - timedelta(hours=2),
        until=now - timedelta(minutes=1),
        mode=MODE_MONITOR_ONLY,
    )

    assert database.claim_due_checks(now.isoformat()) == []
    expired = database.expire_finished_monitors(now.isoformat())
    assert [item.id for item in expired] == [task.id]
    assert database.get_task(task.id, user.id).status == "expired"


def test_finished_and_cancelled_tasks_are_never_checked(tmp_path) -> None:
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    now = datetime.now(UTC)
    booked = _task(database, user.id, start=now - timedelta(minutes=1), until=now + timedelta(hours=2))
    cancelled = _task(database, user.id, start=now - timedelta(minutes=1), until=now + timedelta(hours=2))

    database.update_task_status(booked.id, user.id, "completed", booking_code="1234567890")
    database.update_task_status(cancelled.id, user.id, "cancelled")

    assert database.claim_due_checks(now.isoformat()) == []


def test_a_live_booking_session_pauses_the_poll_loop(tmp_path) -> None:
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    now = datetime.now(UTC)
    task = _task(database, user.id, start=now - timedelta(minutes=1), until=now + timedelta(hours=8))

    assert database.pause_monitoring(task.id, user.id, "waiting_human") is True
    # Long after the interval would have elapsed, still nothing to claim: the
    # loop must not open a second browser while a person is verifying.
    assert database.claim_due_checks((now + timedelta(hours=1)).isoformat()) == []


def test_failures_back_off_and_success_resets(tmp_path) -> None:
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    now = datetime.now(UTC)
    task = _task(
        database,
        user.id,
        start=now - timedelta(minutes=1),
        until=now + timedelta(days=1),
        interval=60,
    )
    database.claim_due_checks(now.isoformat())

    assert database.record_check_failure(task.id, user.id, "upstream 429") == 1
    first_gap = _gap_from_now(database, task, user)
    assert database.record_check_failure(task.id, user.id, "upstream 429") == 2
    second_gap = _gap_from_now(database, task, user)
    assert second_gap > first_gap, "a repeated failure must wait longer, not the same"

    database.clear_check_failures(task.id, user.id)
    assert database.get_task(task.id, user.id).check_failures == 0


def _gap_from_now(database, task, user) -> float:
    stored = database.get_task(task.id, user.id)
    return (
        datetime.fromisoformat(stored.next_check_at) - datetime.now(UTC)
    ).total_seconds()


def test_backoff_is_capped(tmp_path) -> None:
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    now = datetime.now(UTC)
    task = _task(
        database, user.id, start=now - timedelta(minutes=1), until=now + timedelta(days=9), interval=60
    )
    for _ in range(20):
        database.record_check_failure(task.id, user.id, "still broken")
    assert database.get_task(task.id, user.id).check_failures == 8


def test_one_shot_tasks_created_before_monitoring_still_promote(tmp_path) -> None:
    """A task with no monitor window keeps the original fire-once behaviour."""
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    now = datetime.now(UTC)
    task = _task(database, user.id, start=now - timedelta(minutes=1), until=None)

    assert task.monitor_until is None
    promoted = database.promote_due_task_records()
    assert [item.id for item in promoted] == [task.id]
    assert database.get_task(task.id, user.id).status == "waiting_human"
    # And it is not also picked up by the poll loop.
    assert database.claim_due_checks(now.isoformat()) == []


def test_monitor_start_at_is_the_start_not_the_interval(tmp_path) -> None:
    """Guards the field the spec called out: scheduled_at is when, not how often."""
    database = _db(tmp_path)
    user = database.create_user("a@example.com", "hash")
    start = datetime.now(UTC) + timedelta(hours=3)
    task = _task(database, user.id, start=start, until=start + timedelta(hours=2), interval=600)

    assert task.monitor_start_at == task.scheduled_at == start.isoformat()
    assert task.poll_interval_seconds == 600
    # Nothing is due before the start time, however long the interval is.
    assert database.claim_due_checks(datetime.now(UTC).isoformat()) == []
