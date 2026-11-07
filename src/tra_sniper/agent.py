"""What an AI agent (Hermes, Claude, ...) needs to drive bookings over MCP."""

from __future__ import annotations

import hmac
from typing import Any

from starlette.responses import JSONResponse

INSTRUCTIONS = """\
TFRLICKA auto-books Taiwan Railway (台鐵 TRA) tickets.

Flow:
1. find_stations: turn a station name into its value, e.g. "板橋" -> "1020-板橋".
2. search_trains: list the trains for a date between two stations.
3. create_booking_task: book one to three train numbers. Every round the
   server opens the official page in a headless browser, enters the stored
   ID and submits (the page mints its own reCAPTCHA v3 token); success
   records the booking code (電腦代碼) and ends the task. A sold-out round
   waits for the next one. Monitoring stops at monitor_until or the train's
   departure, whichever is first. try_auto_booking forces an attempt now.
4. A round the browser cannot finish sends a webhook reminder carrying
   official_url instead, so nothing is lost. official_url opens TRA's own
   booking page already filled in; always send the person that link --
   never a TFRLICKA dashboard link. It expires within minutes, so call
   get_booking_link for a fresh one whenever the person is about to book.
5. If the person booked by hand, ask for the booking code and call
   report_booked, or cancel_task if they no longer want the trip.

TRA has no public seat-availability data, so a reminder never means a seat is
free; it means "try now". Ticket sales open 28 days before the ride date.
Dates are Taiwan local dates (YYYY-MM-DD); times are Taiwan time (HH:MM).
Without TRA_AUTOBOOK_ENABLED on the server, every round is a reminder.
"""


class BearerGuard:
    """Let only callers holding the agent token reach the wrapped ASGI app."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode() if token else b""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not self.expected or not hmac.compare_digest(supplied, self.expected):
                response = JSONResponse(
                    {"detail": "Set TRA_AGENT_TOKEN and send it as a Bearer token"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
