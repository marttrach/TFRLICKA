from __future__ import annotations

import hmac
import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 15 * 60
TOKEN_BYTES = 32

# Terminal states never resolve a token again; the browser context is gone.
FINISHED_STATUSES = frozenset({"completed", "failed", "timeout", "cancelled"})


class SessionBusyError(RuntimeError):
    """A second concurrent booking session was requested."""

    def __init__(self, active_task_id: str, remaining_seconds: int) -> None:
        super().__init__("another booking session is already active")
        self.active_task_id = active_task_id
        self.remaining_seconds = remaining_seconds


@dataclass(slots=True)
class BookingSession:
    """One remote browser hand-off. The token is the only thing guarding it."""

    token: str
    task_id: str
    user_id: int
    expires_at: datetime
    status: str = "preparing"
    booking_code: str | None = None
    message: str = ""
    stop: threading.Event = field(default_factory=threading.Event)

    def __repr__(self) -> str:
        # The token must never reach a log line, an exception, or a repr in a
        # traceback. Leaking it is equivalent to handing over the browser.
        return (
            f"BookingSession(token='***', task_id={self.task_id!r}, "
            f"user_id={self.user_id!r}, status={self.status!r})"
        )

    def remaining_seconds(self, now: datetime | None = None) -> int:
        delta = self.expires_at - (now or datetime.now(UTC))
        return max(int(delta.total_seconds()), 0)

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.remaining_seconds(now) <= 0


class BookingSessionManager:
    """Exactly one active browser session at a time.

    The sidecar runs a single browser and a human can only solve one reCAPTCHA
    at a time, so this lock is a physical limit, not a policy choice.
    """

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._active: BookingSession | None = None

    def acquire(self, task_id: str, user_id: int) -> BookingSession:
        now = datetime.now(UTC)
        stale: BookingSession | None = None
        with self._lock:
            active = self._active
            if active is not None and not active.is_expired(now):
                raise SessionBusyError(active.task_id, active.remaining_seconds(now))
            stale = active
            session = BookingSession(
                token=secrets.token_urlsafe(TOKEN_BYTES),
                task_id=task_id,
                user_id=user_id,
                expires_at=now + timedelta(seconds=self.ttl_seconds),
            )
            self._active = session
        if stale is not None:
            stale.stop.set()
        return session

    def resolve(self, token: str) -> BookingSession | None:
        """Return the active session when the token matches, else None."""
        with self._lock:
            active = self._active
            if active is None:
                return None
            if not hmac.compare_digest(active.token, token):
                return None
            if active.is_expired() or active.status in FINISHED_STATUSES:
                return None
            return active

    def release(self, token: str) -> BookingSession | None:
        """Drop the session so its token stops resolving, and signal its worker."""
        with self._lock:
            active = self._active
            if active is None or not hmac.compare_digest(active.token, token):
                return None
            self._active = None
        active.stop.set()
        return active

    def reap(self) -> BookingSession | None:
        """Release an expired session. Called from the scheduler tick."""
        with self._lock:
            active = self._active
            if active is None or not active.is_expired():
                return None
            self._active = None
        active.stop.set()
        logger.info(
            "booking session expired and was released",
            extra={"event": "booking_session.reaped", "task_id": active.task_id},
        )
        return active

    @property
    def active(self) -> BookingSession | None:
        with self._lock:
            return self._active


def run_booking_session(
    session: BookingSession,
    *,
    automator: object,
    request: object,
    on_finish: Callable[[BookingSession], None],
) -> None:
    """Drive one booking to its official outcome. Runs in a worker thread.

    The browser stops at the official verification and waits for the person;
    nothing here inspects, solves, or submits the reCAPTCHA.
    """
    try:
        session.status = "waiting_verification"
        result = automator.run(  # type: ignore[attr-defined]
            request,
            submit=True,
            wait_seconds=session.remaining_seconds(),
            stop_event=session.stop,
        )
        session.status = result.status
        session.booking_code = result.booking_code
        session.message = result.message
    except Exception as exc:
        # The worker must never die silently: the person is staring at a browser
        # waiting for an outcome, and the session lock has to be released.
        session.status = "failed"
        session.message = str(exc)
        logger.exception(
            "booking session failed",
            extra={"event": "booking_session.failed", "task_id": session.task_id},
        )
    finally:
        try:
            on_finish(session)
        except Exception:
            logger.exception(
                "booking session cleanup failed",
                extra={"event": "booking_session.cleanup_failed", "task_id": session.task_id},
            )
