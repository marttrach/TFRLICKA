from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .browser_session import BookingSessionManager, SessionBusyError
from .notifications import WebhookNotifier
from .storage import MODE_MONITOR_ONLY, POLLABLE_STATUSES, Database, TaskRecord

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Prepare due bookings, then pause for one human handoff notification."""

    def __init__(
        self,
        database: Database,
        *,
        interval_seconds: float = 5.0,
        notifier: WebhookNotifier | None = None,
        session_manager: BookingSessionManager | None = None,
        prepare_booking: Callable[[TaskRecord], None] | None = None,
    ) -> None:
        self.database = database
        self.interval_seconds = interval_seconds
        self.notifier = notifier or WebhookNotifier.from_env()
        # Reusing this tick as the session reaper avoids a second background
        # thread whose only job would be to check one timestamp.
        self.session_manager = session_manager
        self.prepare_booking = prepare_booking
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        if self.session_manager is not None:
            self.session_manager.reap()

        # Close finished windows before claiming work, so a task whose deadline
        # passed is never checked one last time.
        for expired in self.database.expire_finished_monitors():
            logger.info(
                "monitor window closed",
                extra={"event": "monitor.expired", "task_id": expired.id},
            )

        promoted_tasks = []
        claimed = 0
        for task in self.database.claim_due_checks():
            try:
                if self._run_check(task):
                    promoted_tasks.append(task)
                claimed += 1
            except SessionBusyError:
                # The claim already schedules another attempt at the interval.
                continue
            except Exception:
                record = self.database.get_task(task.id, task.user_id)
                if record is None or record.status not in POLLABLE_STATUSES:
                    continue
                self.database.update_task_status(
                    task.id, task.user_id, "failed", last_error="無法準備訂票頁，請確認設定後重建任務"
                )
                logger.exception("scheduled booking preparation failed")
                if self.notifier.enabled:
                    try:
                        self.notifier.notify_result(task, "failed")
                    except Exception:
                        logger.exception("booking failure notification failed")

        # Claimed, not handed off: a task prepared in a browser is not ready
        # for anyone until its worker says so, and it is counted here anyway
        # because the caller uses this to know the tick did something.
        if claimed:
            logger.info(
                "due tasks claimed",
                extra={"event": "scheduler.tasks_claimed", "claimed_count": claimed},
            )
        if self.notifier.enabled:
            for task in promoted_tasks:
                try:
                    payload = self.database.get_task_payload(task.id, task.user_id)
                    self.notifier.notify(task, payload)
                except Exception:
                    logger.exception(
                        "task webhook failed; task remains ready for human action",
                        extra={"event": "notification.webhook_failed", "task_id": task.id},
                    )
        return claimed

    def _run_check(self, task: TaskRecord) -> bool:
        """Return whether to notify now; browser workers notify when ready."""
        if task.mode != MODE_MONITOR_ONLY and self.prepare_booking is not None:
            self.prepare_booking(task)
            return False
        return self.database.pause_monitoring(task.id, task.user_id, "waiting_human")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tra-task-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.tick()
            except Exception:
                logger.exception(
                    "scheduler tick failed; the scheduler will continue",
                    extra={"event": "scheduler.tick_failed"},
                )
