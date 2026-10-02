import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.storage import Database
from tra_sniper.tdx import TdxClient, TdxError

REDIRECT = "https://maas.transportdata.tw/trip/api/v1/booking/deeplink/redirect?token=abc-123"


class FakeMcp:
    """Answers the three JSON-RPC messages a booking-link request sends."""

    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.text = text
        self.is_error = is_error
        self.calls: list[tuple[dict, dict]] = []

    def __call__(self, message, headers):
        self.calls.append((message, headers))
        if message["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": self.text}], "isError": self.is_error}
            return "session-1", {"jsonrpc": "2.0", "id": message["id"], "result": result}
        return "session-1", None


def _client(tmp_path, mcp) -> TdxClient:
    return TdxClient(client_id="id", client_secret="secret", data_dir=tmp_path, mcp_post=mcp)


def test_booking_link_returns_the_link_a_browser_can_open(tmp_path) -> None:
    # The tool echoes the key-protected API URL it called before the real link.
    mcp = FakeMcp(json.dumps({
        "url": "https://tdx.transportdata.tw/api/maas-tra/booking/deeplink/web/tra?ticket_count=2",
        "deeplink": REDIRECT,
        "expire": "2026-10-01 09:56:00",
    }))
    url = _client(tmp_path, mcp).booking_link("1020-板橋", "1080-桃園", "149", "2026/10/01", 2)

    assert url == REDIRECT
    call, headers = mcp.calls[-1]
    assert call["params"]["arguments"] == {
        "origin_station_name": "板橋",
        "destination_station_name": "桃園",
        "train_number": "149",
        "date": "2026-10-01",
        "ticket_count": 2,
    }
    assert headers == {"cid": "id", "cst": "secret", "mcp-session-id": "session-1"}


@pytest.mark.parametrize("mcp", [
    FakeMcp("查無此車次"),
    FakeMcp(REDIRECT, is_error=True),
])
def test_booking_link_without_a_usable_link_is_an_error(tmp_path, mcp) -> None:
    with pytest.raises(TdxError):
        _client(tmp_path, mcp).booking_link("1020-板橋", "1080-桃園", "149", "2026/10/01", 1)


def _task(client) -> tuple[str, dict[str, str]]:
    registered = client.post(
        "/auth/register", json={"email": "user@example.com", "password": "very-secure-password"}
    )
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    ride_date = (datetime.now().astimezone().date() + timedelta(days=1)).strftime("%Y/%m/%d")
    created = client.post("/tasks", headers=headers, json={
        "scheduled_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "booking": {
            "identity": "A123456789",
            "start_station": "1000-臺北",
            "end_station": "3300-臺中",
            "quantity": 2,
            "outbound": {"ride_date": ride_date, "train_numbers": ["110", "112"]},
        },
    })
    assert created.status_code == 201
    return created.json()["id"], headers


def _api(tmp_path, tdx: TdxClient) -> TestClient:
    database = Database(tmp_path / "api.db", encryption_key=Fernet.generate_key().decode())
    client = TestClient(
        create_app(database, TokenManager("t" * 32), start_scheduler=False, tdx_client=tdx)
    )
    client.database = database
    return client


def test_api_returns_a_link_for_the_tasks_first_train(tmp_path) -> None:
    mcp = FakeMcp(REDIRECT)
    client = _api(tmp_path, _client(tmp_path, mcp))
    task_id, headers = _task(client)

    response = client.post(f"/tasks/{task_id}/booking-link", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"url": REDIRECT, "train_no": "110"}
    arguments = mcp.calls[-1][0]["params"]["arguments"]
    assert (arguments["origin_station_name"], arguments["ticket_count"]) == ("臺北", 2)
    # The traveller's ID never leaves for TDX; they type it on the official page.
    assert "A123456789" not in json.dumps(mcp.calls, ensure_ascii=False)


def test_api_explains_missing_credentials_and_tdx_failures(tmp_path) -> None:
    client = _api(tmp_path, TdxClient(client_id="", client_secret="", data_dir=tmp_path))
    task_id, headers = _task(client)
    assert client.post(f"/tasks/{task_id}/booking-link", headers=headers).status_code == 409
    assert client.post("/tasks/nope/booking-link", headers=headers).status_code == 404

    client.app.state.tdx.client_id = client.app.state.tdx.client_secret = "set"
    client.app.state.tdx.mcp_post = FakeMcp("權限不足", is_error=True)
    assert client.post(f"/tasks/{task_id}/booking-link", headers=headers).status_code == 503


def test_notification_link_needs_no_login_and_redirects_to_a_fresh_tdx_link(tmp_path) -> None:
    mcp = FakeMcp(REDIRECT)
    client = _api(tmp_path, _client(tmp_path, mcp))
    task_id, headers = _task(client)
    user_id = client.get("/auth/me", headers=headers).json()["id"]
    task = client.database.get_task(task_id, user_id)

    notifier = client.app.state.scheduler.notifier
    notifier.public_url = "https://tra.example.test"
    payload = notifier.payload_for(task, {})
    # The message link is the TRA page itself, issued as the reminder goes out.
    assert payload["action_url"] == payload["official_url"] == REDIRECT
    assert len(mcp.calls) == 3  # one TDX booking-link request (3 JSON-RPC messages)

    # booking_url stays valid after the TDX link expires.
    link = payload["booking_url"]
    assert link.startswith(f"https://tra.example.test/api/tasks/{task_id}/booking-link/open?")

    # nginx strips "/api"; the person tapping the link is not logged in.
    path = link.removeprefix("https://tra.example.test/api")
    opened = client.get(path, follow_redirects=False)
    assert (opened.status_code, opened.headers["location"]) == (303, REDIRECT)

    assert client.get(path[:-1] + "0", follow_redirects=False).status_code == 403
    other_task = path.replace(task_id, "another-task")
    assert client.get(other_task, follow_redirects=False).status_code == 403

    client.post(f"/tasks/{task_id}/cancel", headers=headers)
    assert client.get(path, follow_redirects=False).status_code == 410


def test_notification_has_no_booking_link_without_tdx_credentials(tmp_path) -> None:
    client = _api(tmp_path, TdxClient(client_id="", client_secret="", data_dir=tmp_path))
    task_id, headers = _task(client)
    user_id = client.get("/auth/me", headers=headers).json()["id"]
    task = client.database.get_task(task_id, user_id)
    assert "booking_url" not in client.app.state.scheduler.notifier.payload_for(task, {})
