from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .automation import TRCBookingAutomator
from .models import BookingRequest


def load_request(path: str | Path) -> BookingRequest:
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return BookingRequest.from_dict(data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tra-sniper",
        description="Prepare the official TRC individual booking form.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate a booking JSON file")
    validate.add_argument("config")

    subparsers.add_parser("serve", help="Run the local membership and task API")

    book = subparsers.add_parser("book", help="Open and prepare the official booking page")
    book.add_argument("config")
    book.add_argument(
        "--submit",
        action="store_true",
        help="Keep the browser open for manual CAPTCHA and user-confirmed submission",
    )
    book.add_argument("--headless", action="store_true", help="Only valid without --submit")
    book.add_argument("--slow-mo", type=int, default=0, metavar="MS")
    book.add_argument("--wait-seconds", type=int, default=600)
    book.add_argument("--screenshot")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            from .api import run

            run()
            return 0
        request = load_request(args.config)
        if args.command == "validate":
            print(json.dumps(request.redacted(), ensure_ascii=False, indent=2, default=str))
            print("Configuration is valid; identity was redacted.")
            return 0

        automator = TRCBookingAutomator(headless=args.headless, slow_mo_ms=args.slow_mo)
        result = automator.run(
            request,
            submit=args.submit,
            wait_seconds=args.wait_seconds,
            screenshot=args.screenshot,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0 if result.status in {"prepared", "completed"} else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
