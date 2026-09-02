from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from .tdx import TdxClient, TdxError

NO_AVAILABILITY_NOTICE = "僅依官方時刻與車種提供建議；系統不知道任何車次是否有位。"
TRANSFER_NOTICE = (
    "轉乘需分開購買兩張車票；第一段誤點可能影響第二段。系統不知道任一段是否有位，"
    "10 分鐘緩衝亦未計入實際月台距離。"
)
RESERVED_TYPE_CODES = {"1", "2", "3"}
RESERVED_TYPE_NAMES = ("自強", "普悠瑪", "太魯閣")

# Ordered major hubs on the western trunk line. Only points strictly between
# the selected stations are considered, so branch and eastern routes degrade
# to direct suggestions instead of inventing a transfer path.
WESTERN_HUBS = (
    ("1000", "臺北"),
    ("1020", "板橋"),
    ("1080", "桃園"),
    ("1210", "新竹"),
    ("1250", "竹南"),
    ("3160", "苗栗"),
    ("3230", "豐原"),
    ("3300", "臺中"),
    ("3360", "彰化"),
    ("4080", "嘉義"),
    ("4220", "臺南"),
    ("4340", "新左營"),
    ("4400", "高雄"),
)


def time_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


@dataclass(frozen=True, slots=True)
class TrainCandidate:
    train_no: str
    train_type_name: str
    departure_time: str
    arrival_time: str
    duration_minutes: int
    is_reserved_type: bool
    seat_type_label: str
    note: str
    in_requested_window: bool = False


def _localized(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("Zh_tw") or value.get("En") or "")
    return str(value or "")


def _stop_times(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    stops = record.get("StopTimes")
    if isinstance(stops, list) and len(stops) >= 2:
        return stops[0], stops[-1]
    origin = record.get("OriginStopTime")
    destination = record.get("DestinationStopTime")
    if isinstance(origin, dict) and isinstance(destination, dict):
        return origin, destination
    raise ValueError("timetable record has no usable stop times")


def parse_candidate(record: dict[str, Any], start: int, end: int) -> TrainCandidate:
    info = record.get("TrainInfo") or record.get("DailyTrainInfo") or {}
    if not isinstance(info, dict):
        raise TypeError("timetable record has no train information")
    origin, destination = _stop_times(record)
    departure = str(origin.get("DepartureTime") or origin.get("ArrivalTime") or "")
    arrival = str(destination.get("ArrivalTime") or destination.get("DepartureTime") or "")
    depart_minutes = time_minutes(departure)
    arrival_minutes = time_minutes(arrival)
    if arrival_minutes < depart_minutes:
        arrival_minutes += 24 * 60
    type_name = _localized(info.get("TrainTypeName"))
    type_code = str(info.get("TrainTypeCode") or info.get("TrainTypeID") or "")
    is_reserved = type_code in RESERVED_TYPE_CODES or any(
        name in type_name for name in RESERVED_TYPE_NAMES
    )
    note = _localized(info.get("Note"))
    return TrainCandidate(
        train_no=str(info.get("TrainNo", "")),
        train_type_name=type_name or "未標示車種",
        departure_time=departure,
        arrival_time=arrival,
        duration_minutes=arrival_minutes - depart_minutes,
        is_reserved_type=is_reserved,
        seat_type_label="對號列車" if is_reserved else "非對號列車",
        note=note,
        in_requested_window=start <= depart_minutes <= end,
    )


def candidates_from_records(
    records: list[dict[str, Any]], start_time: str, end_time: str
) -> list[TrainCandidate]:
    start = time_minutes(start_time)
    end = time_minutes(end_time)
    candidates: list[TrainCandidate] = []
    for record in records:
        try:
            candidate = parse_candidate(record, start, end)
        except (TypeError, ValueError):
            continue
        departure = time_minutes(candidate.departure_time)
        if start - 60 <= departure <= end + 60:
            candidates.append(candidate)
    return candidates


def sort_candidates(
    candidates: list[TrainCandidate], start_time: str, end_time: str, *, prefer_reserved: bool
) -> list[TrainCandidate]:
    midpoint = (time_minutes(start_time) + time_minutes(end_time)) / 2
    return sorted(
        candidates,
        key=lambda item: (
            not item.in_requested_window,
            item.is_reserved_type != prefer_reserved,
            item.duration_minutes,
            abs(time_minutes(item.departure_time) - midpoint),
        ),
    )


def _station_id(value: str) -> str:
    return value.partition("-")[0].strip()


def transfer_hubs(start_id: str, end_id: str) -> list[tuple[str, str]]:
    ids = [item[0] for item in WESTERN_HUBS]
    if start_id not in ids or end_id not in ids:
        return []
    start_index, end_index = ids.index(start_id), ids.index(end_id)
    low, high = sorted((start_index, end_index))
    hubs = list(WESTERN_HUBS[low + 1 : high])
    if start_index > end_index:
        hubs.reverse()
    return hubs[:6]


def pair_transfers(
    first_legs: list[TrainCandidate],
    second_legs: list[TrainCandidate],
    *,
    transfer_station: dict[str, str],
    direct_fastest_minutes: int,
    buffer_minutes: int = 10,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    maximum = direct_fastest_minutes * 1.5
    for first in first_legs:
        first_departure = time_minutes(first.departure_time)
        first_arrival = time_minutes(first.arrival_time)
        if first_arrival < first_departure:
            first_arrival += 24 * 60
        for second in second_legs:
            second_departure = time_minutes(second.departure_time)
            while second_departure < first_arrival:
                second_departure += 24 * 60
            if second_departure < first_arrival + buffer_minutes:
                continue
            second_arrival = time_minutes(second.arrival_time)
            while second_arrival < second_departure:
                second_arrival += 24 * 60
            total = second_arrival - first_departure
            if total > maximum:
                continue
            results.append(
                {
                    "transfer_station": transfer_station,
                    "departure_time": first.departure_time,
                    "arrival_time": second.arrival_time,
                    "duration_minutes": total,
                    "buffer_minutes": second_departure - first_arrival,
                    "first_leg": asdict(first),
                    "second_leg": asdict(second),
                    "notice": TRANSFER_NOTICE,
                }
            )
    return sorted(results, key=lambda item: (item["duration_minutes"], -item["buffer_minutes"]))


class SuggestionService:
    def __init__(self, client: TdxClient) -> None:
        self.client = client

    def suggest(
        self,
        *,
        start_station: str,
        end_station: str,
        ride_date: str,
        start_time: str,
        end_time: str,
        prefer_reserved: bool = True,
        include_transfers: bool = True,
    ) -> dict[str, Any]:
        date.fromisoformat(ride_date.replace("/", "-"))
        start_id, end_id = _station_id(start_station), _station_id(end_station)
        direct_records = self.client.daily_timetable(start_id, end_id, ride_date)
        direct = candidates_from_records(direct_records, start_time, end_time)
        ranked = sort_candidates(direct, start_time, end_time, prefer_reserved=prefer_reserved)
        primary = [item for item in ranked if item.in_requested_window and item.is_reserved_type]
        alternatives = [
            item
            for item in ranked
            if (item.in_requested_window and not item.is_reserved_type)
            or (not item.in_requested_window and item.is_reserved_type)
        ]
        transfers: list[dict[str, Any]] = []
        all_direct = candidates_from_records(direct_records, "00:00", "23:59")
        fastest = min((item.duration_minutes for item in all_direct), default=0)
        if include_transfers and fastest:
            for hub_id, hub_name in transfer_hubs(start_id, end_id):
                try:
                    first = candidates_from_records(
                        self.client.daily_timetable(start_id, hub_id, ride_date),
                        start_time,
                        end_time,
                    )
                    second = candidates_from_records(
                        self.client.daily_timetable(hub_id, end_id, ride_date),
                        "00:00",
                        "23:59",
                    )
                except TdxError:
                    # Direct candidates remain useful if a secondary OD query fails.
                    continue
                transfers.extend(
                    pair_transfers(
                        first,
                        second,
                        transfer_station={"value": f"{hub_id}-{hub_name}", "label": hub_name},
                        direct_fastest_minutes=fastest,
                    )
                )
        return {
            "primary": [asdict(item) for item in primary],
            "alternatives": [asdict(item) for item in alternatives],
            "transfers": sorted(
                transfers, key=lambda item: (item["duration_minutes"], -item["buffer_minutes"])
            )[:3],
            "availability_known": False,
            "notice": NO_AVAILABILITY_NOTICE,
        }
