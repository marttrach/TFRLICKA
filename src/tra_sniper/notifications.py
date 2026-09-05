from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.request
from collections.abc import Callable
from typing import Any

from .storage import TaskRecord

NOTICE = "候選僅為時刻建議，不代表有位；驗證碼與送出仍須人工完成於官方頁面"
RESULT_NOTICE = "訂位成功，請於台鐵規定期限內完成付款取票"
AUTH_HEADER = "X-TRA-Auth"

# The static auth header is only adequate because notifications carry no
# session token and no link that triggers an action by itself. Someone holding
# the token can forge a message, not start a booking. If a session URL or a
# one-click trigger is ever added to a payload, this stops being true and the
# receiver must verify X-TRA-Signature instead. See PLAN.md 12.9.
Sender = Callable[[str, bytes, dict[str, str], float], None]


def _default_sender(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"webhook returned HTTP {response.status}")


def _candidate_rows(payload: dict[str, Any]) -> list[dict[str, str]]:
    suggestions = payload.get("candidate_suggestions")
    if not isinstance(suggestions, dict):
        return []
    result: list[dict[str, str]] = []
    for group_name in ("primary", "alternatives"):
        group = suggestions.get(group_name)
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "train_no": str(item.get("train_no", "")),
                    "depart": str(item.get("departure_time", "")),
                    "seat_type": str(item.get("seat_type_label", "")),
                }
            )
            if len(result) == 3:
                return result
    return result


class WebhookNotifier:
    """Send privacy-minimised task-ready notifications to an HTTP webhook."""

    def __init__(
        self,
        url: str = "",
        secret: str = "",
        public_url: str = "http://localhost:43124",
        *,
        timeout_seconds: float = 5.0,
        sender: Sender | None = None,
        auth_token: str = "",
    ) -> None:
        self.url = url.strip()
        self.secret = secret
        self.public_url = public_url.strip().rstrip("/")
        self.timeout_seconds = timeout_seconds
        # Static header for receivers that cannot verify the HMAC, such as an
        # n8n instance that blocks $env access in Code nodes. It is a SEPARATE
        # value from `secret` on purpose: this one travels in cleartext on every
        # request, so leaking it must not also compromise the signing key.
        self.auth_token = auth_token.strip()
        self._sender = sender or _default_sender

    @classmethod
    def from_env(cls) -> WebhookNotifier:
        return cls(
            url=os.getenv("TRA_WEBHOOK_URL", ""),
            secret=os.getenv("TRA_WEBHOOK_SECRET", ""),
            public_url=os.getenv("TRA_PUBLIC_URL", "http://localhost:43124"),
            auth_token=os.getenv("TRA_WEBHOOK_AUTH_TOKEN", ""),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.secret)

    def payload_for(self, task: TaskRecord, stored_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "event": "task.waiting_human",
            "task_id": task.id,
            "route": task.route,
            "ride_date": task.ride_date,
            "candidates": _candidate_rows(stored_payload),
            "action_url": f"{self.public_url}/tasks/{task.id}",
            "note": NOTICE,
        }

    def result_payload_for(
        self, task: TaskRecord, status: str, booking_code: str | None
    ) -> dict[str, Any]:
        return {
            "event": "task.booking_result",
            "task_id": task.id,
            "route": task.route,
            "ride_date": task.ride_date,
            "status": status,
            "booking_code": booking_code,
            "note": RESULT_NOTICE if status == "completed" else NOTICE,
        }

    def notify_result(
        self, task: TaskRecord, status: str, booking_code: str | None = None
    ) -> bool:
        return self._post(self.result_payload_for(task, status, booking_code))

    def notify(self, task: TaskRecord, stored_payload: dict[str, Any]) -> bool:
        return self._post(self.payload_for(task, stored_payload))

    def _post(self, payload: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-TRA-Signature": f"sha256={signature}",
        }
        if self.auth_token:
            headers[AUTH_HEADER] = self.auth_token
        self._sender(self.url, body, headers, self.timeout_seconds)
        return True
