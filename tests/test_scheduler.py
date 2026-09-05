import logging
import threading
from types import SimpleNamespace

from tra_sniper.scheduler import TaskScheduler


class QuietMonitorMixin:
    """The monitoring queries a tick makes beyond promotion."""

    def expire_finished_monitors(self) -> list[object]:
        return []

    def claim_due_checks(self) -> list[object]:
        return []


class FlakyDatabase(QuietMonitorMixin):
    def __init__(self) -> None:
        self.calls = 0
        self.recovered = threading.Event()

    def claim_due_checks(self) -> list[object]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("database is locked")
        self.recovered.set()
        return []


def test_scheduler_survives_a_failed_tick(caplog) -> None:
    database = FlakyDatabase()
    scheduler = TaskScheduler(database, interval_seconds=0.01)  # type: ignore[arg-type]
    caplog.set_level(logging.ERROR, logger="tra_sniper.scheduler")
    scheduler.start()
    try:
        assert database.recovered.wait(timeout=1)
    finally:
        scheduler.stop()
    assert database.calls >= 2
    assert "scheduler tick failed" in caplog.text


def test_scheduler_tick_returns_promoted_count() -> None:
    class DatabaseStub(QuietMonitorMixin):
        def claim_due_checks(self) -> list[object]:
            return [SimpleNamespace(id=str(i), user_id=1, mode="monitor_only") for i in range(3)]

        def pause_monitoring(self, task_id, user_id, status):
            return True

    scheduler = TaskScheduler(DatabaseStub())  # type: ignore[arg-type]
    assert scheduler.tick() == 3
