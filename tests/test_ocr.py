from io import BytesIO

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.ocr import OcrResult, OcrService
from tra_sniper.storage import Database


class FakeOcrService:
    def recognize(self, image_data: bytes, language: str = "zh-TW") -> OcrResult:
        assert image_data == b"fake-image"
        return OcrResult(text="台鐵 TRA 123", language=language, width=640, height=320)


def _token(client: TestClient) -> str:
    response = client.post(
        "/auth/register",
        json={"email": "ocr@example.com", "password": "very-secure-password"},
    )
    return response.json()["access_token"]


def test_ocr_endpoint_requires_authentication_and_returns_text(tmp_path) -> None:
    app = create_app(
        Database(tmp_path / "ocr.db", encryption_key=Fernet.generate_key().decode()),
        TokenManager("t" * 32),
        FakeOcrService(),
        start_scheduler=False,
    )

    with TestClient(app) as client:
        anonymous = client.post(
            "/ocr",
            files={"image": ("sample.png", b"fake-image", "image/png")},
        )
        assert anonymous.status_code == 401

        token = _token(client)
        response = client.post(
            "/ocr",
            headers={"Authorization": f"Bearer {token}"},
            data={"language": "zh-TW"},
            files={"image": ("sample.png", b"fake-image", "image/png")},
        )

    assert response.status_code == 200
    assert response.json() == {
        "text": "台鐵 TRA 123",
        "language": "zh-TW",
        "width": 640,
        "height": 320,
    }


def test_ocr_endpoint_rejects_unsupported_media_type(tmp_path) -> None:
    app = create_app(
        Database(tmp_path / "ocr.db", encryption_key=Fernet.generate_key().decode()),
        TokenManager("t" * 32),
        FakeOcrService(),
        start_scheduler=False,
    )
    with TestClient(app) as client:
        token = _token(client)
        response = client.post(
            "/ocr",
            headers={"Authorization": f"Bearer {token}"},
            files={"image": ("sample.svg", b"<svg/>", "image/svg+xml")},
        )
    assert response.status_code == 415


def test_ocr_service_preprocesses_image(monkeypatch) -> None:
    buffer = BytesIO()
    Image.new("RGB", (120, 60), "white").save(buffer, format="PNG")

    def fake_image_to_string(image, **kwargs) -> str:
        assert image.mode == "L"
        assert kwargs["lang"] == "chi_tra+eng"
        assert kwargs["timeout"] == 30
        return "  車次 110  \n"

    monkeypatch.setattr("tra_sniper.ocr.pytesseract.image_to_string", fake_image_to_string)
    result = OcrService().recognize(buffer.getvalue())

    assert result.text == "車次 110"
    assert (result.width, result.height) == (120, 60)
