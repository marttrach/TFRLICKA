from __future__ import annotations

import logging
import threading
from typing import Any

from .notifications import WebhookNotifier
from .storage import MODE_AUTO_SUBMIT, MODE_MONITOR_ONLY, Database, TaskRecord

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Send a booking reminder, carrying the official link, for every due round.

    No seat-availability source exists, so a round cannot know whether a seat
    freed up; it reminds the person, whose browser does the booking. Reminders
    repeat every poll interval until the task is booked, cancelled or its
    window closes. monitor_only reminds once. auto_submit asks the browser
    submitter to book on the person's behalf first; the reminder only goes out
    when that attempt could not finish the job.
    """

    def __init__(
        self,
        database: Database,
        *,
        interval_seconds: float = 5.0,
        notifier: WebhookNotifier | None = None,
        autobook: Any | None = None,
    ) -> None:
        self.database = database
        self.interval_seconds = interval_seconds
        self.notifier = notifier or WebhookNotifier.from_env()
        # Anything with submit(url, identity) -> SubmitResult; None disables
        # auto_submit mode, whose tasks then behave like book_when_available.
        self.autobook = autobook
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
        if task.mode == MODE_AUTO_SUBMIT and self.autobook is not None and self._try_autobook(task):
            return False
        if task.mode == MODE_MONITOR_ONLY:
            # Reminding once means leaving the poll loop after this round.
            return self.database.pause_monitoring(task.id, task.user_id, "waiting_human")
        return True

    def _try_autobook(self, task: TaskRecord) -> bool:
        """Book through the official page; True means the task is completed.

        Every failure path — no identity, no link, browser trouble, an
        unrecognised page — lands in the same place: log why, keep the task in
        the poll loop, and let the reminder reach a human instead. Auto-submit
        may fail, but it may not lose the booking.
        """
        assert self.autobook is not None
        try:
            payload = self.database.get_task_payload(task.id, task.user_id)
            identity = str(payload.get("identity", "")).strip()
            official_url = self.notifier.official_url_for(task) if self.notifier.official_url_for else None
            if not identity or not official_url:
                reason = "任務沒有身分證資料" if not identity else "無法取得官方訂票連結"
                self.database.update_task_status(task.id, task.user_id, task.status, last_error=f"自動訂票：{reason}，改發提醒")
                return False
            result = self.autobook.submit(official_url, identity)
        except Exception as exc:
            logger.exception(
                "auto-submit attempt failed; the reminder covers for it",
                extra={"event": "autobook.attempt_failed", "task_id": task.id},
            )
            try:
                self.database.update_task_status(
                    task.id, task.user_id, task.status,
                    last_error=f"自動訂票出錯：{str(exc)[:80]}，改發提醒",
                )
            except Exception:  # noqa: BLE001, S110 - bookkeeping must never outrank the reminder
                pass
            return False

        if result.ok:
            code = result.booking_code or "AUTO"
            self.database.update_task_status(
                task.id, task.user_id, "completed", booking_code=code, last_error=None
            )
            booked = self.database.get_task(task.id, task.user_id)
            if booked is not None and self.notifier.enabled:
                try:
                    self.notifier.notify_result(booked, "completed", booked.booking_code)
                except Exception:
                    logger.exception(
                        "auto-book result webhook failed",
                        extra={"event": "notification.webhook_failed", "task_id": task.id},
                    )
            logger.info(
                "auto-submit booked the ticket",
                extra={"event": "autobook.completed", "task_id": task.id, "booking_code": code},
            )
            return True

        logger.info(
            "auto-submit did not finish; reminding the person instead",
            extra={
                "event": "autobook.fell_back",
                "task_id": task.id,
                "reason": result.reason,
            },
        )
        try:
            self.database.update_task_status(
                task.id, task.user_id, task.status,
                last_error=f"自動訂票未完成（{result.reason}），改發提醒",
            )
        except Exception:  # noqa: BLE001, S110 - bookkeeping must never outrank the reminder
            pass
        return False

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
