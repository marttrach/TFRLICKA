"""What an AI agent (Hermes, Claude, ...) needs to drive bookings over MCP."""

from __future__ import annotations

import hmac
from typing import Any

from starlette.responses import JSONResponse

INSTRUCTIONS = """\
TFRLICKA books Taiwan Railway (台鐵 TRA) tickets with a person in the loop.
You can do everything up to the official booking page; the person finishes it.

Flow:
1. find_stations: turn a station name into its value, e.g. "板橋" -> "1020-板橋".
2. search_trains: list the trains for a date between two stations.
3. create_booking_task: watch one to three train numbers. Every round it sends a
   webhook reminder whose booking_url opens the official page already filled
   with date, stations, train and ticket count.
4. To book right now, call get_booking_link and send the official_url to the
   person. It expires within minutes; booking_url never does while the task
   is open.
5. On the official page the person enters their national ID, passes the
   verification (CAPTCHA) and presses 訂票. Nobody else can do that step.
6. Ask the person for the booking code (電腦代碼) and call report_booked, or
   cancel_task if they no longer want the trip.

TRA has no public seat-availability data, so a reminder never means a seat is
free; it means "try now". Ticket sales open 28 days before the ride date.
Dates are Taiwan local dates (YYYY-MM-DD); times are Taiwan time (HH:MM).
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
