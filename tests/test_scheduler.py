import logging
import threading

from tra_sniper.scheduler import TaskScheduler


class FlakyDatabase:
    def __init__(self) -> None:
        self.calls = 0
        self.recovered = threading.Event()

    def promote_due_task_records(self) -> list[object]:
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
    class DatabaseStub:
        def promote_due_task_records(self) -> list[object]:
            return [object(), object(), object()]

    scheduler = TaskScheduler(DatabaseStub())  # type: ignore[arg-type]
    assert scheduler.tick() == 3
