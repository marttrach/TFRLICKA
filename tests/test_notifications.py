from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tra_sniper.models import BookingRequest
from tra_sniper.notifications import NOTICE, WebhookNotifier
from tra_sniper.scheduler import TaskScheduler
from tra_sniper.storage import Database, TaskRecord


def task_record() -> TaskRecord:
    return TaskRecord(
        id="task-123",
        user_id=7,
        status="waiting_human",
        scheduled_at="2026-11-20T00:00:00+00:00",
        route="臺北 → 臺中",
        ride_date="2026/11/21",
        order_type="BY_TIME",
        created_at="2026-11-19T00:00:00+00:00",
        updated_at="2026-11-20T00:00:00+00:00",
        last_error=None,
    )


def test_webhook_signs_minimal_payload_and_excludes_secrets() -> None:
    sent: dict[str, Any] = {}

    def sender(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
        sent.update(url=url, body=body, headers=headers, timeout=timeout)

    notifier = WebhookNotifier(
        "https://hooks.example.test/tra",
        "notification-secret",
        "https://tra.example.test",
        sender=sender,
    )
    stored = {
        "identity": "A123456789",
        "member_login": {"account": "member", "password": "private"},
        "candidate_suggestions": {
            "primary": [
                {
                    "train_no": "109",
                    "departure_time": "08:13",
                    "seat_type_label": "對號列車",
                    "arrival_time": "10:00",
                }
            ],
            "alternatives": [],
        },
    }

    assert notifier.notify(task_record(), stored) is True
    decoded = json.loads(sent["body"])
    assert decoded == {
        "event": "task.waiting_human",
        "task_id": "task-123",
        "route": "臺北 → 臺中",
        "ride_date": "2026/11/21",
        "candidates": [{"train_no": "109", "depart": "08:13", "seat_type": "對號列車"}],
        "action_url": "https://tra.example.test/tasks/task-123",
        "note": NOTICE,
    }
    assert b"A123456789" not in sent["body"]
    assert b"private" not in sent["body"]
    expected = hmac.new(b"notification-secret", sent["body"], hashlib.sha256).hexdigest()
    assert sent["headers"]["X-TRA-Signature"] == f"sha256={expected}"


def test_booking_result_payload_is_signed_and_excludes_identity() -> None:
    sent: dict[str, Any] = {}

    def sender(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
        sent.update(url=url, body=body, headers=headers, timeout=timeout)

    notifier = WebhookNotifier(
        "https://hooks.example.test/tra",
        "notification-secret",
        "https://tra.example.test",
        sender=sender,
    )

    assert notifier.notify_result(task_record(), "completed", "1234567890") is True

    payload = json.loads(sent["body"])
    assert payload["event"] == "task.booking_result"
    assert payload["booking_code"] == "1234567890"
    assert "A123456789" not in sent["body"].decode("utf-8")
    assert "identity" not in payload

    # R-6 keeps the existing HMAC signing rather than a static header token.
    expected = hmac.new(b"notification-secret", sent["body"], hashlib.sha256).hexdigest()
    assert sent["headers"]["X-TRA-Signature"] == f"sha256={expected}"


def test_disabled_webhook_does_not_call_sender() -> None:
    called = False

    def sender(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
        nonlocal called
        called = True

    assert WebhookNotifier(sender=sender).notify(task_record(), {}) is False
    assert called is False


def test_scheduler_posts_to_real_local_http_server(tmp_path) -> None:
    received: dict[str, Any] = {}
    ready = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size = int(self.headers["Content-Length"])
            received["body"] = self.rfile.read(size)
            received["signature"] = self.headers["X-TRA-Signature"]
            self.send_response(204)
            self.end_headers()
            ready.set()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        database = Database(tmp_path / "notify.db")
        user = database.create_user("notify@example.com", "hash")
        ride_date = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
        booking_data = {
            "identity": "A123456789",
            "start_station": "1000-臺北",
            "end_station": "3300-臺中",
            "outbound": {"ride_date": ride_date, "train_numbers": ["109"]},
        }
        task = database.create_task(
            user.id,
            BookingRequest.from_dict(booking_data),
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            booking_data,
        )
        port = server.server_address[1]
        notifier = WebhookNotifier(
            f"http://127.0.0.1:{port}/hook",
            "integration-secret",
            "http://nas.local:43124",
        )

        assert TaskScheduler(database, notifier=notifier).tick() == 1
        assert ready.wait(timeout=1)
        assert database.get_task(task.id, user.id).status == "waiting_human"
        decoded = json.loads(received["body"])
        assert decoded["task_id"] == task.id
        assert decoded["action_url"] == f"http://nas.local:43124/tasks/{task.id}"
        assert "identity" not in decoded
        expected = hmac.new(
            b"integration-secret", received["body"], hashlib.sha256
        ).hexdigest()
        assert received["signature"] == f"sha256={expected}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_notification_failure_does_not_roll_back_promotion(tmp_path) -> None:
    database = Database(tmp_path / "failed-notify.db")
    user = database.create_user("failure@example.com", "hash")
    ride_date = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    booking_data = {
        "identity": "A123456789",
        "start_station": "1000-臺北",
        "end_station": "3300-臺中",
        "outbound": {"ride_date": ride_date, "train_numbers": ["109"]},
    }
    task = database.create_task(
        user.id,
        BookingRequest.from_dict(booking_data),
        (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        booking_data,
    )

    def fail(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
        raise OSError("webhook unavailable")

    notifier = WebhookNotifier("https://hooks.invalid", "secret", sender=fail)
    assert TaskScheduler(database, notifier=notifier).tick() == 1
    assert database.get_task(task.id, user.id).status == "waiting_human"
