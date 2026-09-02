from __future__ import annotations

import re
from dataclasses import dataclass

from .ocr import OcrResult, OcrService

MANUAL_REVIEW_NOTICE = "辨識結果僅供整理與複製，請對照原圖人工核對。"
NO_CHALLENGE_NOTICE = "此功能不辨識 CAPTCHA 或 reCAPTCHA，也不會送出訂位。"

TRAIN_NUMBER_PATTERNS = (
    re.compile(
        r"(?:車次|列車)\s*(?:號碼|編號|No\.?|NO\.?)?\s*[:：#]?\s*([A-Z]?\d{1,4})",
        re.IGNORECASE,
    ),
    re.compile(r"(?:Train\s*No\.?)\s*[:：#]?\s*([A-Z]?\d{1,4})", re.IGNORECASE),
)
DATE_PATTERN = re.compile(
    r"(?<!\d)(?:20\d{2}|1\d{2})[./-](?:0?[1-9]|1[0-2])[./-](?:0?[1-9]|[12]\d|3[01])(?!\d)"
)
TIME_PATTERN = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3])[:：][0-5]\d(?!\d)")


@dataclass(frozen=True, slots=True)
class TraDocumentFields:
    document_type: str
    train_numbers: tuple[str, ...]
    stations: tuple[str, ...]
    dates: tuple[str, ...]
    times: tuple[str, ...]
    route: str | None


@dataclass(frozen=True, slots=True)
class TraOcrResult:
    text: str
    language: str
    width: int
    height: int
    fields: TraDocumentFields
    warnings: tuple[str, ...] = (MANUAL_REVIEW_NOTICE, NO_CHALLENGE_NOTICE)


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _document_type(text: str) -> str:
    if any(keyword in text for keyword in ("車票", "票價", "座位", "車廂", "取票")):
        return "ticket"
    if any(keyword in text for keyword in ("時刻", "到達", "抵達", "出發", "車次")):
        return "timetable"
    return "unknown"


def extract_tra_fields(text: str, station_names: list[str] | tuple[str, ...]) -> TraDocumentFields:
    normalized = text.replace("：", ":").replace("／", "/")
    train_numbers: list[str] = []
    for pattern in TRAIN_NUMBER_PATTERNS:
        train_numbers.extend(match.upper() for match in pattern.findall(normalized))

    positions: list[tuple[int, str]] = []
    for name in sorted(set(station_names), key=len, reverse=True):
        if len(name) < 2:
            continue
        index = normalized.find(name)
        if index >= 0:
            positions.append((index, name))
    stations = _unique([name for _, name in sorted(positions)])
    dates = _unique(DATE_PATTERN.findall(normalized))
    times = _unique(value.replace("：", ":") for value in TIME_PATTERN.findall(normalized))
    route = f"{stations[0]} → {stations[1]}" if len(stations) >= 2 else None
    return TraDocumentFields(
        document_type=_document_type(normalized),
        train_numbers=_unique(train_numbers),
        stations=stations,
        dates=dates,
        times=times,
        route=route,
    )


class TraOcrService:
    """Recognize ordinary TRA documents and extract fields for manual review."""

    def __init__(self, ocr_service: OcrService | None = None) -> None:
        self.ocr_service = ocr_service or OcrService()

    def recognize(
        self,
        image_data: bytes,
        *,
        station_names: list[str] | tuple[str, ...],
        language: str = "zh-TW",
    ) -> TraOcrResult:
        result: OcrResult = self.ocr_service.recognize(image_data, language)
        return TraOcrResult(
            text=result.text,
            language=result.language,
            width=result.width,
            height=result.height,
            fields=extract_tra_fields(result.text, station_names),
        )
