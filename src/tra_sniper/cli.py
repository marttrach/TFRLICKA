from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import BookingRequest
from .ocr import SUPPORTED_LANGUAGES, OcrService


def load_request(path: str | Path) -> BookingRequest:
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return BookingRequest.from_dict(data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tra-sniper",
        description="Taiwan Railway booking reminders with a person in the loop.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate a booking JSON file")
    validate.add_argument("config")

    subparsers.add_parser("serve", help="Run the local membership and task API")

    ocr = subparsers.add_parser("ocr", help="Recognize text in a local image")
    ocr.add_argument("image")
    ocr.add_argument(
        "--language",
        choices=SUPPORTED_LANGUAGES,
        default="zh-TW",
        help="OCR language preset (default: zh-TW)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            from .api import run

            run()
            return 0
        if args.command == "ocr":
            result = OcrService().recognize(Path(args.image).read_bytes(), args.language)
            print(result.text)
            return 0
        request = load_request(args.config)
        print(json.dumps(request.redacted(), ensure_ascii=False, indent=2, default=str))
        print("Configuration is valid; identity was redacted.")
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
