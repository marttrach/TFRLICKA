from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import pytesseract
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
SUPPORTED_LANGUAGES = {
    "zh-TW": "chi_tra+eng",
    "en": "eng",
}


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    language: str
    width: int
    height: int


class OcrService:
    """Recognize text in user-supplied images without retaining the image."""

    def recognize(self, image_data: bytes, language: str = "zh-TW") -> OcrResult:
        if not image_data:
            raise ValueError("Image is empty")
        if len(image_data) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 8 MB limit")
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError("Unsupported OCR language")

        try:
            with Image.open(BytesIO(image_data)) as source:
                if source.format not in SUPPORTED_IMAGE_FORMATS:
                    raise ValueError("Only PNG, JPEG, and WebP images are supported")
                width, height = source.size
                if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("Image dimensions are invalid or too large")
                image = ImageOps.exif_transpose(source).convert("L")
                image = ImageOps.autocontrast(image)
                image.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("The uploaded file is not a valid image") from exc

        try:
            text = pytesseract.image_to_string(
                image,
                lang=SUPPORTED_LANGUAGES[language],
                config="--oem 1 --psm 6",
                timeout=30,
            ).strip()
        except pytesseract.TesseractNotFoundError as exc:
            raise RuntimeError("Tesseract OCR is not installed") from exc
        except RuntimeError as exc:
            raise RuntimeError("OCR processing timed out or failed") from exc

        return OcrResult(text=text, language=language, width=width, height=height)
