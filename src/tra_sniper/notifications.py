from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from .storage import TaskRecord

NOTICE = "候選僅為時刻建議，不代表有位；驗證碼與送出仍須人工完成於官方頁面"
RESULT_NOTICE = "訂位成功，請於台鐵規定期限內完成付款取票"
TOKEN_HEADER = "X-TRA-Token"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# The receiver authenticates with a shared token instead of verifying a
# signature, so the token is a bearer credential: whoever holds it can post a
# notification. That is acceptable only because payloads carry no session token
# and no link that triggers an action by itself, so a forged message misleads
# but cannot start a booking. Adding either to a payload invalidates this and
# the receiver would need a verifiable signature again. See PLAN.md 12.9.
Sender = Callable[[str, bytes, dict[str, str], float], None]


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the token header would go to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise RuntimeError(
            f"webhook responded with redirect HTTP {code}; refusing to forward the token"
        )


_OPENER = urllib.request.build_opener(_RefuseRedirect)


def _default_sender(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with _OPENER.open(request, timeout=timeout) as response:
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
    ) -> None:
        self.url = url.strip()
        # TRA_WEBHOOK_SECRET now carries the Header Auth token rather than a
        # signing key. Sent verbatim: no "Bearer " and no "sha256=" prefix.
        self.secret = secret
        self.public_url = public_url.strip().rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._sender = sender or _default_sender

    @classmethod
    def from_env(cls) -> WebhookNotifier:
        return cls(
            url=os.getenv("TRA_WEBHOOK_URL", ""),
            secret=os.getenv("TRA_WEBHOOK_SECRET", ""),
            public_url=os.getenv("TRA_PUBLIC_URL", "http://localhost:43124"),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.secret)

    @property
    def transport_is_safe(self) -> bool:
        """The token is a bearer credential, so cleartext to a remote host leaks it."""
        parsed = urlparse(self.url)
        return parsed.scheme == "https" or (parsed.hostname or "") in LOOPBACK_HOSTS

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
        if not self.transport_is_safe:
            # Raised, not silently skipped: the caller logs it and the task is
            # unaffected, so a misconfigured URL is visible instead of quietly
            # putting the token on the wire in cleartext.
            raise RuntimeError(
                "webhook URL must use HTTPS (or point at loopback); "
                "the auth token would otherwise be sent in cleartext"
            )
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            TOKEN_HEADER: self.secret,
        }
        self._sender(self.url, body, headers, self.timeout_seconds)
        return True
