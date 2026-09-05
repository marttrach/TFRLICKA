import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest

from tra_sniper.browser_session import (
    BookingSessionManager,
    SessionBusyError,
    run_booking_session,
)


class FakeAutomator:
    """Stands in for Playwright; records how run() was called."""

    def __init__(self, result, block: threading.Event | None = None) -> None:
        self.result = result
        self.block = block
        self.calls: list[dict] = []

    def run(self, request, **kwargs):
        self.calls.append({"request": request, **kwargs})
        if self.block is not None:
            self.block.wait(timeout=5)
        return self.result


class FakeResult:
    def __init__(self, status="completed", booking_code="1234567890", message="ok") -> None:
        self.status = status
        self.booking_code = booking_code
        self.message = message


def test_session_token_is_not_predictable() -> None:
    manager = BookingSessionManager()
    first = manager.acquire("task-1", 1)
    manager.release(first.token)
    second = manager.acquire("task-2", 1)

    assert len(first.token) >= 43
    assert first.token != second.token


def test_second_concurrent_session_is_refused() -> None:
    manager = BookingSessionManager()
    first = manager.acquire("task-1", 1)

    with pytest.raises(SessionBusyError) as excinfo:
        manager.acquire("task-2", 1)

    assert excinfo.value.active_task_id == "task-1"
    assert excinfo.value.remaining_seconds > 0
    # The refusal must not disturb the session that holds the lock.
    assert manager.resolve(first.token) is first


def test_wrong_token_does_not_resolve() -> None:
    manager = BookingSessionManager()
    session = manager.acquire("task-1", 1)

    assert manager.resolve("x" * len(session.token)) is None
    assert manager.resolve(session.token) is session


def test_expired_session_is_rejected_and_reaped() -> None:
    manager = BookingSessionManager(ttl_seconds=0)
    session = manager.acquire("task-1", 1)

    assert session.is_expired()
    assert manager.resolve(session.token) is None

    reaped = manager.reap()
    assert reaped is session
    assert session.stop.is_set(), "reaping must signal the worker to close the browser"
    assert manager.active is session
    with pytest.raises(SessionBusyError):
        manager.acquire("task-2", 2)
    manager.release(session.token)  # Worker has closed the browser.
    assert manager.active is None


def test_released_session_cannot_be_reused() -> None:
    manager = BookingSessionManager()
    session = manager.acquire("task-1", 1)

    assert manager.release(session.token) is session
    assert manager.resolve(session.token) is None
    assert manager.release(session.token) is None


def test_finished_session_stops_resolving() -> None:
    manager = BookingSessionManager()
    session = manager.acquire("task-1", 1)
    session.status = "completed"

    assert manager.resolve(session.token) is None


def test_lock_is_free_again_after_release() -> None:
    manager = BookingSessionManager()
    first = manager.acquire("task-1", 1)
    manager.release(first.token)

    second = manager.acquire("task-2", 2)
    assert second.task_id == "task-2"


def test_session_token_never_appears_in_repr_or_logs(caplog) -> None:
    manager = BookingSessionManager(ttl_seconds=0)
    session = manager.acquire("task-1", 1)

    assert session.token not in repr(session)
    assert "***" in repr(session)

    with caplog.at_level(logging.INFO):
        manager.reap()
    assert session.token not in caplog.text


def test_run_booking_session_records_result_and_finishes() -> None:
    manager = BookingSessionManager()
    session = manager.acquire("task-1", 1)
    automator = FakeAutomator(FakeResult())
    finished: list = []

    run_booking_session(
        session,
        automator=automator,
        request={"booking": True},
        on_finish=finished.append,
    )

    assert session.status == "completed"
    assert session.booking_code == "1234567890"
    assert finished == [session]
    # submit=True keeps the browser on the page for the person to verify; the
    # automator is never asked to solve or bypass anything.
    assert automator.calls[0]["submit"] is True
    assert automator.calls[0]["stop_event"] is session.stop


def test_run_booking_session_survives_automator_failure() -> None:
    class Exploding:
        def run(self, *args, **kwargs):
            raise RuntimeError("browser exploded")

    manager = BookingSessionManager()
    session = manager.acquire("task-1", 1)
    finished: list = []

    run_booking_session(
        session,
        automator=Exploding(),
        request={},
        on_finish=finished.append,
    )

    assert session.status == "failed"
    assert "browser exploded" in session.message
    assert finished == [session], "cleanup must run even when the browser fails"


def test_remaining_seconds_never_goes_negative() -> None:
    manager = BookingSessionManager()
    session = manager.acquire("task-1", 1)
    session.expires_at = datetime.now(UTC) - timedelta(minutes=5)

    assert session.remaining_seconds() == 0
    assert session.is_expired()


def test_login_and_booking_handoffs_notify_once_even_when_notification_fails() -> None:
    manager = BookingSessionManager()
    session = manager.acquire("task-1", 1)
    notifications = []

    def notify(ready):
        notifications.append(ready.status)
        raise OSError("webhook unavailable")

    class ReadyTwice:
        def run(self, request, **kwargs):
            assert session.status == "preparing"
            kwargs["on_ready"]()  # Member login needs a person.
            kwargs["on_ready"]()  # Booking form later needs the same person.
            assert session.status == "waiting_verification"
            return FakeResult()

    run_booking_session(session, automator=ReadyTwice(), request={},
                        on_ready=notify, on_finish=lambda finished: manager.release(finished.token))
    assert notifications == ["waiting_verification"]
    assert session.status == "completed"
    assert manager.active is None
