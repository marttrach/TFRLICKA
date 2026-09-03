from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Protocol
from urllib.parse import urlparse


class VerificationMode(str, Enum):
    MANUAL = "manual"
    MOCK = "mock"
    OFFICIAL_API = "official_api"


@dataclass(frozen=True, slots=True)
class VerificationCapabilities:
    mode: VerificationMode
    ready: bool
    automated: bool
    production_allowed: bool
    message: str

    def as_dict(self) -> dict[str, str | bool]:
        return asdict(self)


class VerificationProvider(Protocol):
    @property
    def capabilities(self) -> VerificationCapabilities: ...

    def authorize(self, *, target_url: str, challenge_token: str | None = None) -> str | None: ...


class ManualVerificationProvider:
    @property
    def capabilities(self) -> VerificationCapabilities:
        return VerificationCapabilities(
            mode=VerificationMode.MANUAL,
            ready=True,
            automated=False,
            production_allowed=True,
            message="由使用者在官方頁面完成驗證",
        )

    def authorize(self, *, target_url: str, challenge_token: str | None = None) -> None:
        del target_url, challenge_token


class MockVerificationProvider:
    """Local-only adapter used to develop the future verification hand-off contract."""

    LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.getenv("TRA_MOCK_VERIFICATION_TOKEN", "local-test-approved")

    @property
    def capabilities(self) -> VerificationCapabilities:
        return VerificationCapabilities(
            mode=VerificationMode.MOCK,
            ready=True,
            automated=True,
            production_allowed=False,
            message="僅供本機測試，不會連線或處理正式台鐵驗證",
        )

    def authorize(self, *, target_url: str, challenge_token: str | None = None) -> str:
        del challenge_token
        hostname = (urlparse(target_url).hostname or "").lower()
        if hostname not in self.LOCAL_HOSTS:
            raise ValueError("mock verification is restricted to localhost")
        return self.token


class OfficialApiVerificationProvider:
    """Stable placeholder for a future TRC-approved verification API adapter."""

    @property
    def capabilities(self) -> VerificationCapabilities:
        return VerificationCapabilities(
            mode=VerificationMode.OFFICIAL_API,
            ready=False,
            automated=True,
            production_allowed=False,
            message="等待台鐵提供核准的 API 規格、端點與認證方式",
        )

    def authorize(self, *, target_url: str, challenge_token: str | None = None) -> str:
        del target_url, challenge_token
        raise RuntimeError("official verification API adapter is not configured")


def create_verification_provider(mode: str | None = None) -> VerificationProvider:
    configured = (mode or os.getenv("TRA_VERIFICATION_PROVIDER", "manual")).strip().lower()
    try:
        selected = VerificationMode(configured)
    except ValueError as exc:
        choices = ", ".join(item.value for item in VerificationMode)
        raise ValueError(f"TRA_VERIFICATION_PROVIDER must be one of: {choices}") from exc
    if selected is VerificationMode.MANUAL:
        return ManualVerificationProvider()
    if selected is VerificationMode.MOCK:
        return MockVerificationProvider()
    return OfficialApiVerificationProvider()
