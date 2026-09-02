import json
import logging

from tra_sniper.logging_config import JsonFormatter


def test_json_formatter_only_includes_allowlisted_context() -> None:
    record = logging.LogRecord("tra_sniper.test", logging.INFO, __file__, 1, "done", (), None)
    record.event = "test.completed"
    record.duration_ms = 12.5
    record.identity = "A123456789"

    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "test.completed"
    assert payload["duration_ms"] == 12.5
    assert "identity" not in payload
