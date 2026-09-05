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
# How long a signalled worker gets to close its browser context before the
# slot is taken back. Without this a worker that never reaches on_finish
# blocks every future session until the API restarts.
CLEANUP_GRACE_SECONDS = 60

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
    stopped_at: datetime | None = None
    # Whether the browser ever reached a point a person could take over.
    # A round that never got there failed structurally, and retrying it
    # would just hammer a page we cannot fill.
    handed_off: bool = False

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
        with self._lock:
            active = self._active
            # Only the worker can release the slot, after its context closes.
            if active is not None:
                raise SessionBusyError(active.task_id, active.remaining_seconds(now))
            session = BookingSession(
                token=secrets.token_urlsafe(TOKEN_BYTES),
                task_id=task_id,
                user_id=user_id,
                expires_at=now + timedelta(seconds=self.ttl_seconds),
            )
            self._active = session
        return session

    def resolve(self, token: str) -> BookingSession | None:
        """Return the active session when the token matches, else None."""
        with self._lock:
            active = self._active
            if active is None:
                return None
            if not hmac.compare_digest(active.token, token):
                return None
            if active.stop.is_set() or active.is_expired() or active.status in FINISHED_STATUSES:
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
        """Stop an expired session, then take its slot back if it never cleans up."""
        now = datetime.now(UTC)
        with self._lock:
            active = self._active
            if active is None or not active.is_expired(now):
                return None
            if active.stop.is_set():
                stopped_at = active.stopped_at
                if stopped_at is None:
                    return None
                if (now - stopped_at).total_seconds() < CLEANUP_GRACE_SECONDS:
                    return None
                self._active = None
                logger.warning(
                    "booking session slot force-released; its worker never finished",
                    extra={
                        "event": "booking_session.force_released",
                        "task_id": active.task_id,
                    },
                )
                return active
            active.stopped_at = now
        active.stop.set()
        logger.info(
            "booking session expired; waiting for browser cleanup",
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
    on_ready: Callable[[BookingSession], None] | None = None,
) -> None:
    """Drive one booking to its official outcome. Runs in a worker thread.

    The browser stops at the official verification and waits for the person;
    nothing here inspects, solves, or submits the reCAPTCHA.
    """
    def ready() -> None:
        # Login and booking can both need a person. Notify once for this session,
        # even if the automator reports both handoff points or the webhook fails.
        if session.status != "preparing" or session.stop.is_set() or session.is_expired():
            return
        session.status = "waiting_verification"
        session.handed_off = True
        if on_ready:
            try:
                on_ready(session)
            except Exception:
                logger.exception("booking handoff notification failed")

    try:
        result = automator.run(  # type: ignore[attr-defined]
            request,
            submit=True,
            wait_seconds=session.remaining_seconds(),
            stop_event=session.stop,
            on_ready=ready,
        )
        session.status = result.status
        if session.status == "cancelled" and session.is_expired():
            session.status = "timeout"
        session.booking_code = result.booking_code
        session.message = result.message
    except Exception as exc:
        # The worker must never die silently: the person is staring at a browser
        # waiting for an outcome, and the session lock has to be released.
        session.status = "failed"
        if session.is_expired():
            session.status = "timeout"
        elif session.stop.is_set():
            session.status = "cancelled"
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
