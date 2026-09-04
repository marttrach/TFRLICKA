from __future__ import annotations

import logging
import threading

from .browser_session import BookingSessionManager
from .notifications import WebhookNotifier
from .storage import Database

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Promotes due tasks to a human-action queue; it never solves CAPTCHA."""

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
        promoted_tasks = self.database.promote_due_task_records()
        promoted = len(promoted_tasks)
        if promoted:
            logger.info(
                "scheduled tasks are ready for human action",
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
