from __future__ import annotations

import logging
import threading

from .notifications import WebhookNotifier
from .storage import MODE_MONITOR_ONLY, Database, TaskRecord

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Send a booking reminder, carrying the official link, for every due round.

    No seat-availability source exists, so a round cannot know whether a seat
    freed up; it reminds the person, whose browser does the booking. Reminders
    repeat every poll interval until the task is booked, cancelled or its
    window closes. monitor_only reminds once.
    """

    def __init__(
        self,
        database: Database,
        *,
        interval_seconds: float = 5.0,
        notifier: WebhookNotifier | None = None,
    ) -> None:
        self.database = database
        self.interval_seconds = interval_seconds
        self.notifier = notifier or WebhookNotifier.from_env()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        # Close finished windows before claiming work, so a task whose deadline
        # passed is never reminded one last time.
        for expired in self.database.expire_finished_monitors():
            logger.info(
                "monitor window closed",
                extra={"event": "monitor.expired", "task_id": expired.id},
            )

        # Claiming already schedules the next round, so a reminder that fails
        # to send is simply retried one interval later.
        due = [task for task in self.database.claim_due_checks() if self._run_check(task)]
        if due:
            logger.info(
                "due tasks claimed",
                extra={"event": "scheduler.tasks_claimed", "claimed_count": len(due)},
            )
        if self.notifier.enabled:
            for task in due:
                try:
                    payload = self.database.get_task_payload(task.id, task.user_id)
                    self.notifier.notify(task, payload)
                except Exception:
                    logger.exception(
                        "task webhook failed; the next round reminds again",
                        extra={"event": "notification.webhook_failed", "task_id": task.id},
                    )
        return len(due)

    def _run_check(self, task: TaskRecord) -> bool:
        """Return whether to remind now."""
        if task.mode == MODE_MONITOR_ONLY:
            # Reminding once means leaving the poll loop after this round.
            return self.database.pause_monitoring(task.id, task.user_id, "waiting_human")
        return True

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
