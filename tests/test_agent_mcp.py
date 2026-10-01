"""An agent can run the whole flow over MCP, up to the person's own booking step."""

import json
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.models import TAIWAN_TZ
from tra_sniper.storage import Database

TOKEN = "agent-token-for-tests"
OFFICIAL = "https://maas.transportdata.tw/trip/api/v1/booking/deeplink/redirect?token=t-1"


class FakeTdx:
    configured = True

    def __init__(self) -> None:
        self.links: list[tuple] = []

    def stations(self, fallback):
        return list(fallback)

    def load_cached_stations(self):
        return []

    def daily_timetable(self, start_id, end_id, ride_date):
        def record(no, name, code, departure, arrival):
            return {
                "TrainInfo": {"TrainNo": no, "TrainTypeCode": code,
                              "TrainTypeName": {"Zh_tw": name}},
                "StopTimes": [{"DepartureTime": departure}, {"ArrivalTime": arrival}],
            }
        return [
            record("152", "自強", "3", "09:10", "11:00"),
            record("110", "自強", "3", "08:00", "10:00"),
            record("2100", "區間", "6", "08:30", "11:40"),
        ]

    def booking_link(self, *args):
        self.links.append(args)
        return OFFICIAL


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRA_AGENT_TOKEN", TOKEN)
    monkeypatch.setenv("TRA_AGENT_EMAIL", "me@example.com")
    database = Database(tmp_path / "agent.db", encryption_key=Fernet.generate_key().decode())
    tdx = FakeTdx()
    app = create_app(database, TokenManager("t" * 32), tdx_client=tdx, start_scheduler=False)
    with TestClient(app) as test_client:
        test_client.post(
            "/auth/register",
            json={"email": "me@example.com", "password": "very-secure-password"},
        )
        test_client.tdx = tdx
        yield test_client


def rpc(client, method, params=None, token=TOKEN):
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    return response


def call(client, tool, **arguments):
    reply = rpc(client, "tools/call", {"name": tool, "arguments": arguments}).json()["result"]
    text = reply["content"][0]["text"] if reply["content"] else "null"
    if reply.get("isError"):
        raise AssertionError(text)
    structured = reply.get("structuredContent")
    if structured is not None:
        return structured.get("result", structured)
    return json.loads(text)


def test_the_endpoint_needs_the_agent_token(client) -> None:
    assert rpc(client, "tools/list", token="wrong").status_code == 401
    hello = rpc(client, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "hermes", "version": "1"},
    }).json()["result"]
    assert "report_booked" in hello["instructions"]
    names = {tool["name"] for tool in rpc(client, "tools/list").json()["result"]["tools"]}
    assert names == {
        "find_stations", "search_trains", "create_booking_task", "list_tasks",
        "get_booking_link", "report_booked", "cancel_task",
    }


def test_an_agent_runs_the_flow_up_to_the_persons_booking(client) -> None:
    assert call(client, "find_stations", keyword="台北")[0]["value"] == "1000-臺北"

    ride = (datetime.now(TAIWAN_TZ).date() + timedelta(days=3)).isoformat()
    trains = call(client, "search_trains", from_station="1000-臺北",
                  to_station="3300-臺中", date=ride, start_time="08:00", end_time="10:00")
    assert [train["train_no"] for train in trains] == ["110", "2100", "152"]

    task = call(client, "create_booking_task", from_station="1000-臺北",
                to_station="3300-臺中", date=ride, train_numbers=["110"], quantity=2)
    assert task["status"] == "scheduled"
    assert f"/api/tasks/{task['task_id']}/booking-link/open?" in task["booking_url"]
    assert [item["task_id"] for item in call(client, "list_tasks")] == [task["task_id"]]

    link = call(client, "get_booking_link", task_id=task["task_id"])
    assert link == {"official_url": OFFICIAL, "train_no": "110"}
    assert client.tdx.links[-1] == ("1000-臺北", "3300-臺中", "110", ride.replace("-", "/"), 2)

    done = call(client, "report_booked", task_id=task["task_id"], booking_code="ab123456")
    assert (done["status"], done["booking_code"], done["booking_url"]) == (
        "completed", "AB123456", None
    )
    with pytest.raises(AssertionError, match="cannot be cancelled"):
        call(client, "cancel_task", task_id=task["task_id"])


def test_tool_errors_carry_the_dashboard_message(client) -> None:
    with pytest.raises(AssertionError, match="出發站與抵達站不可相同"):
        call(client, "search_trains", from_station="1000-臺北", to_station="1000-臺北",
             date=(datetime.now(TAIWAN_TZ).date() + timedelta(days=1)).isoformat())
