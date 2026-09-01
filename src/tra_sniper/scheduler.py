from __future__ import annotations

import threading

from .storage import Database


class TaskScheduler:
    """Promotes due tasks to a human-action queue; it never solves CAPTCHA."""

    def __init__(self, database: Database, *, interval_seconds: float = 5.0) -> None:
        self.database = database
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        return self.database.promote_due_tasks()

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
            self.tick()
