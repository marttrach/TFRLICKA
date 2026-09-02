import json
from datetime import datetime, timedelta

from tra_sniper.cli import main


def test_validate_command_redacts_identity(tmp_path, capsys) -> None:
    config = tmp_path / "booking.json"
    config.write_text(
        json.dumps(
            {
                "identity": "A123456789",
                "start_station": "1000-臺北",
                "end_station": "3300-臺中",
                "outbound": {
                    "ride_date": (
                        datetime.now().astimezone().date() + timedelta(days=1)
                    ).strftime("%Y/%m/%d"),
                    "train_numbers": ["110"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert main(["validate", str(config)]) == 0
    output = capsys.readouterr().out
    assert "A123456789" not in output
    assert "***789" in output
