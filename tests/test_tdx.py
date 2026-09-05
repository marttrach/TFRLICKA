from __future__ import annotations

import json

from tra_sniper.api import POPULAR_STATIONS
from tra_sniper.tdx import STATION_COUNTIES, HttpError, TdxClient


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
            return {
                "Stations": [
                    {
                        "StationID": "1000",
                        "StationName": {"Zh_tw": "臺北"},
                        "LocationCity": "臺北市",
                    },
                    {
                        "StationID": "1020",
                        "StationName": {"Zh_tw": "板橋"},
                        "StationAddress": "新北市板橋區縣民大道二段7號",
                    },
                ]
            }
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
    fetched = client.fetch_stations()
    assert fetched == [
        {"value": "1000-臺北", "label": "臺北", "county": "臺北市"},
        # No LocationCity, so the county comes from the address prefix.
        {"value": "1020-板橋", "label": "板橋", "county": "新北市"},
    ]
    cached = json.loads((tmp_path / "stations.json").read_text(encoding="utf-8"))
    assert cached[0]["label"] == "臺北"
    assert cached[0]["county"] == "臺北市"

    fallback = [{"value": "3300-臺中", "label": "臺中", "county": "臺中市"}]
    offline = TdxClient(client_id="", client_secret="", data_dir=tmp_path / "empty")
    assert offline.stations(fallback) == fallback


def test_county_less_cache_is_grouped_without_reaching_tdx(tmp_path) -> None:
    """The reported bug: every station fell into 其他 and the picker died.

    A cache written before counties existed, plus credentials that are unset or
    broken, used to serve blank counties forever. The frontend then filed every
    station under 其他, which collapsed the two-level picker into the same flat
    list as the "search all stations" toggle, so that toggle did nothing.
    """
    stale = [{"value": "1000-臺北", "label": "臺北"}, {"value": "4400-高雄", "label": "高雄"}]
    (tmp_path / "stations.json").write_text(json.dumps(stale), encoding="utf-8")

    offline = TdxClient(client_id="", client_secret="", data_dir=tmp_path)
    assert offline.stations([]) == [
        {"value": "1000-臺北", "label": "臺北", "county": "臺北市"},
        {"value": "4400-高雄", "label": "高雄", "county": "高雄市"},
    ]


def test_shipped_county_wins_over_tdx(tmp_path) -> None:
    """TDX renaming or emptying its county field cannot break the picker."""
    cache = [{"value": "1000-臺北", "label": "臺北", "county": ""}]
    (tmp_path / "stations.json").write_text(json.dumps(cache), encoding="utf-8")

    offline = TdxClient(client_id="", client_secret="", data_dir=tmp_path)
    assert offline.stations([])[0]["county"] == "臺北市"


def test_station_opened_after_the_table_keeps_the_tdx_county(tmp_path) -> None:
    cache = [{"value": "9999-新站", "label": "新站", "county": "宜蘭縣"}]
    (tmp_path / "stations.json").write_text(json.dumps(cache), encoding="utf-8")

    offline = TdxClient(client_id="", client_secret="", data_dir=tmp_path)
    assert offline.stations([])[0]["county"] == "宜蘭縣"


def test_shipped_counties_match_the_hand_written_ones() -> None:
    """The shipped table is scraped data, so anchor it to values written by hand.

    These fourteen span every region, so a mis-parsed table cannot pass.
    """
    for station in POPULAR_STATIONS:
        station_id = station["value"].split("-", 1)[0]
        assert STATION_COUNTIES[station_id] == station["county"], station["label"]


def test_shipped_counties_are_well_formed() -> None:
    assert len(STATION_COUNTIES) > 200
    assert all(key.isdigit() and len(key) == 4 for key in STATION_COUNTIES)
    # Taiwan has 22 divisions; the four with no TRA line must not appear.
    assert all(value.endswith(("市", "縣")) for value in STATION_COUNTIES.values())
    assert not {"南投縣", "澎湖縣", "金門縣", "連江縣"} & set(STATION_COUNTIES.values())
