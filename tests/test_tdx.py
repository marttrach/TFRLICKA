from __future__ import annotations

import json

from tra_sniper.tdx import HttpError, TdxClient


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.timetable_failures = 0

    def request_json(self, method, url, *, headers=None, form=None):
        self.calls.append((method, url))
        if method == "POST":
            assert form["client_secret"] == "secret"
            return {"access_token": "token", "expires_in": 3600}
        if url.endswith("/Station"):
            return {"Stations": [{"StationID": "1000", "StationName": {"Zh_tw": "臺北"}}]}
        if self.timetable_failures:
            self.timetable_failures -= 1
            raise HttpError(429)
        return {"TrainTimetables": [{"TrainInfo": {"TrainNo": "110"}}]}


def test_token_and_timetable_are_cached(tmp_path) -> None:
    transport = FakeTransport()
    client = TdxClient(
        client_id="id",
        client_secret="secret",
        data_dir=tmp_path,
        transport=transport,
    )
    first = client.daily_timetable("1000", "3300", "2026-09-03")
    second = client.daily_timetable("1000", "3300", "2026/09/03")
    assert first == second
    assert len(transport.calls) == 2  # one token request and one timetable request


def test_429_uses_bounded_exponential_backoff(tmp_path) -> None:
    transport = FakeTransport()
    transport.timetable_failures = 2
    sleeps: list[float] = []
    client = TdxClient(
        client_id="id",
        client_secret="secret",
        data_dir=tmp_path,
        transport=transport,
        sleep=sleeps.append,
    )
    assert client.daily_timetable("1000", "3300", "2026-09-03")
    assert sleeps == [4.0, 8.0]


def test_station_cache_and_unconfigured_fallback(tmp_path) -> None:
    transport = FakeTransport()
    client = TdxClient(
        client_id="id",
        client_secret="secret",
        data_dir=tmp_path,
        transport=transport,
    )
    assert client.fetch_stations() == [{"value": "1000-臺北", "label": "臺北"}]
    assert json.loads((tmp_path / "stations.json").read_text(encoding="utf-8"))[0]["label"] == "臺北"

    fallback = [{"value": "3300-臺中", "label": "臺中"}]
    offline = TdxClient(client_id="", client_secret="", data_dir=tmp_path / "empty")
    assert offline.stations(fallback) == fallback
