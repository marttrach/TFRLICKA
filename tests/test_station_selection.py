import pytest

from tra_sniper.automation import choose_station_suggestion


def test_full_station_value_is_exact() -> None:
    suggestions = ["1000-臺北", "1001-臺北-環島"]
    assert choose_station_suggestion("1000-臺北", suggestions) == 0


def test_unique_station_name_is_selected() -> None:
    assert choose_station_suggestion("臺中", ["3300-臺中", "3340-新烏日"]) == 0


def test_ambiguous_station_requires_code() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        choose_station_suggestion("臺北", ["1000-臺北", "1001-臺北"])


def test_unknown_station_is_rejected() -> None:
    with pytest.raises(ValueError, match="not found"):
        choose_station_suggestion("不存在", ["1000-臺北"])
