from __future__ import annotations

import logging
import threading

from .browser_session import BookingSessionManager
from .notifications import WebhookNotifier
from .storage import MODE_MONITOR_ONLY, Database

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Runs the monitoring poll loop; it never solves CAPTCHA.

    A "check" cannot report seat availability: TDX publishes no TRA seat data
    (PLAN.md 7.1) and polling the booking site is forbidden by development
    constraints 4 and 6. A check reports that a task's monitoring window is
    open and, in book_when_available mode, hands it to a person to verify.
    """

    def __init__(
        self,
        database: Database,
        *,
        interval_seconds: float = 5.0,
        notifier: WebhookNotifier | None = None,
        session_manager: BookingSessionManager | None = None,
    ) -> None:
        self.database = database
        self.interval_seconds = interval_seconds
        self.notifier = notifier or WebhookNotifier.from_env()
        # Reusing this tick as the session reaper avoids a second background
        # thread whose only job would be to check one timestamp.
        self.session_manager = session_manager
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

        promoted_tasks = self.database.promote_due_task_records()
        for task in self.database.claim_due_checks():
            self._run_check(task)
            promoted_tasks.append(task)

        promoted = len(promoted_tasks)
        if promoted:
            logger.info(
                "tasks are ready for human action",
                extra={"event": "scheduler.tasks_promoted", "promoted_count": promoted},
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
        return promoted

    def _run_check(self, task) -> None:  # type: ignore[no-untyped-def]
        """Record one monitoring check.

        There is nothing to query yet: no authorised seat-availability source
        exists, so a check cannot decide "there is a seat". It records that the
        window is open and, in book_when_available mode, moves the task to
        waiting_human so the person can open a booking session. Claiming the
        task already pushed next_check_at forward, so a check that finds
        nothing simply waits for the next interval.
        """
        self.database.clear_check_failures(task.id, task.user_id)
        if task.mode == MODE_MONITOR_ONLY:
            return
        # Leaving the pollable statuses is what stops the loop from opening a
        # second browser while the first one is still waiting for a person.
        self.database.pause_monitoring(task.id, task.user_id, "waiting_human")

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
