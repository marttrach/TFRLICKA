from tra_sniper.ocr import OcrResult
from tra_sniper.tra_ocr import TraOcrService, extract_tra_fields


class FakeOcr:
    def recognize(self, image_data: bytes, language: str) -> OcrResult:
        assert image_data == b"ordinary-tra-screenshot"
        return OcrResult(
            text="台鐵車票\n車次：110\n臺北 → 臺中\n2026/09/03 08:30 10:10\n座位 3車12號",
            language=language,
            width=1280,
            height=720,
        )


def test_extracts_tra_document_fields_without_guessing() -> None:
    fields = extract_tra_fields(
        "時刻表 車次 110 臺北 08：30 → 臺中 10:10 2026-09-03",
        ["臺北", "板橋", "臺中"],
    )
    assert fields.document_type == "timetable"
    assert fields.train_numbers == ("110",)
    assert fields.stations == ("臺北", "臺中")
    assert fields.dates == ("2026-09-03",)
    assert fields.times == ("08:30", "10:10")
    assert fields.route == "臺北 → 臺中"


def test_tra_ocr_wraps_general_ocr_and_requires_manual_review() -> None:
    result = TraOcrService(FakeOcr()).recognize(
        b"ordinary-tra-screenshot",
        station_names=["臺北", "臺中"],
    )
    assert result.fields.document_type == "ticket"
    assert result.fields.train_numbers == ("110",)
    assert result.fields.route == "臺北 → 臺中"
    assert any("人工核對" in warning for warning in result.warnings)
    assert any("reCAPTCHA" in warning for warning in result.warnings)
