from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
API_ROOT = "https://tdx.transportdata.tw/api/basic/v3/Rail/TRA"


class TdxError(RuntimeError):
    """A safe, credential-free error exposed to callers."""


class HttpError(TdxError):
    def __init__(self, status: int, message: str = "TDX request failed") -> None:
        super().__init__(message)
        self.status = status


class HttpTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> Any: ...


class UrllibTransport:
    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> Any:
        data = urllib.parse.urlencode(form).encode() if form else None
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise HttpError(exc.code) from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise TdxError("TDX is temporarily unavailable") from exc


def _county_of(record: dict[str, Any]) -> str:
    """Read the station's county from TDX, falling back to its address prefix."""
    city = record.get("LocationCity")
    if isinstance(city, str) and city.strip():
        return city.strip()
    address = record.get("StationAddress")
    if isinstance(address, str):
        # Addresses start with the county, e.g. "臺北市中正區黎明里...".
        for width in (3, 4):
            head = address.strip()[:width]
            if head.endswith(("市", "縣")):
                return head
    return ""


@dataclass(slots=True)
class CacheEntry:
    value: Any
    expires_at: float


class TdxClient:
    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        data_dir: str | Path | None = None,
        cache_ttl_hours: float | None = None,
        transport: HttpTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client_id = client_id if client_id is not None else os.getenv("TDX_CLIENT_ID", "")
        self.client_secret = (
            client_secret if client_secret is not None else os.getenv("TDX_CLIENT_SECRET", "")
        )
        self.data_dir = Path(data_dir or os.getenv("TRA_DATA_DIR", "./data")).resolve()
        configured_ttl = cache_ttl_hours
        if configured_ttl is None:
            configured_ttl = float(os.getenv("TDX_CACHE_TTL_HOURS", "6"))
        self.cache_ttl_seconds = max(configured_ttl, 0.01) * 3600
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self.sleep = sleep
        self._token: CacheEntry | None = None
        self._timetables: dict[tuple[str, str, str], CacheEntry] = {}

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def station_cache_path(self) -> Path:
        return self.data_dir / "stations.json"

    def _access_token(self) -> str:
        now = self.clock()
        if self._token and self._token.expires_at > now:
            return str(self._token.value)
        if not self.configured:
            raise TdxError("TDX credentials are not configured")
        payload = self.transport.request_json(
            "POST",
            TOKEN_URL,
            form={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        try:
            token = str(payload["access_token"])
            expires_in = max(int(payload.get("expires_in", 300)), 61)
        except (KeyError, TypeError, ValueError) as exc:
            raise TdxError("TDX returned an invalid token response") from exc
        self._token = CacheEntry(token, now + expires_in - 60)
        return token

    def _get(self, path: str) -> Any:
        for attempt in range(3):
            try:
                return self.transport.request_json(
                    "GET",
                    f"{API_ROOT}/{path}",
                    headers={"Authorization": f"Bearer {self._access_token()}"},
                )
            except HttpError as exc:
                if exc.status == 401:
                    self._token = None
                if exc.status != 429 or attempt == 2:
                    raise TdxError("TDX is temporarily unavailable") from exc
                # TDX testing showed that calls less than four seconds apart can
                # remain throttled, so use four seconds as the exponential base.
                self.sleep(float(4 * 2**attempt))
        raise TdxError("TDX is temporarily unavailable")

    @staticmethod
    def _records(payload: Any, *keys: str) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        raise TdxError("TDX returned an unexpected response")

    def daily_timetable(self, start_id: str, end_id: str, ride_date: str) -> list[dict[str, Any]]:
        normalized_date = ride_date.replace("/", "-")
        key = (start_id, end_id, normalized_date)
        now = self.clock()
        cached = self._timetables.get(key)
        if cached and cached.expires_at > now:
            return cached.value
        payload = self._get(
            f"DailyTrainTimetable/OD/{start_id}/to/{end_id}/{normalized_date}"
        )
        records = self._records(payload, "TrainTimetables", "DailyTrainTimetables")
        self._timetables[key] = CacheEntry(records, now + self.cache_ttl_seconds)
        return records

    def fetch_stations(self) -> list[dict[str, str]]:
        payload = self._get("Station")
        records = self._records(payload, "Stations")
        stations: list[dict[str, str]] = []
        for record in records:
            station_id = str(record.get("StationID", "")).strip()
            name_value = record.get("StationName", {})
            name = name_value.get("Zh_tw", "") if isinstance(name_value, dict) else ""
            if station_id and name:
                stations.append(
                    {
                        "value": f"{station_id}-{name}",
                        "label": str(name),
                        # Grouping comes from the source, never guessed from the
                        # station name: "臺北" is in 臺北市 but "新烏日" is not in
                        # 新北市. Blank when TDX omits it, and the UI then files
                        # the station under "其他".
                        "county": _county_of(record),
                    }
                )
        if not stations:
            raise TdxError("TDX returned an empty station list")
        stations.sort(key=lambda item: item["value"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.station_cache_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(stations, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.station_cache_path)
        return stations

    def load_cached_stations(self) -> list[dict[str, str]]:
        try:
            payload = json.loads(self.station_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        # A cache written before counties existed simply has no "county" key;
        # it stays usable and those stations group under "其他".
        return [
            {
                "value": str(item["value"]),
                "label": str(item["label"]),
                "county": str(item.get("county", "")),
            }
            for item in payload
            if isinstance(item, dict) and item.get("value") and item.get("label")
        ]

    def stations(self, fallback: list[dict[str, str]]) -> list[dict[str, str]]:
        cached = self.load_cached_stations()
        if cached:
            return _popular_first(cached, fallback)
        if self.configured:
            try:
                return _popular_first(self.fetch_stations(), fallback)
            except TdxError:
                pass
        return list(fallback)


def _popular_first(
    stations: list[dict[str, str]], popular: list[dict[str, str]]
) -> list[dict[str, str]]:
    weights = {item["value"]: index for index, item in enumerate(popular)}
    return sorted(
        stations,
        key=lambda item: (weights.get(item["value"], len(weights)), item["label"]),
    )
