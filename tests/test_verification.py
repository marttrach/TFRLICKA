import pytest

from tra_sniper.verification import (
    ManualVerificationProvider,
    MockVerificationProvider,
    OfficialApiVerificationProvider,
    VerificationMode,
    create_verification_provider,
)


def test_manual_provider_is_the_safe_default(monkeypatch) -> None:
    monkeypatch.delenv("TRA_VERIFICATION_PROVIDER", raising=False)
    provider = create_verification_provider()
    assert isinstance(provider, ManualVerificationProvider)
    assert provider.capabilities.mode is VerificationMode.MANUAL
    assert provider.capabilities.production_allowed is True
    assert provider.authorize(target_url="https://www.trc.com.tw/") is None


def test_mock_provider_is_restricted_to_localhost() -> None:
    provider = MockVerificationProvider("approved-for-test")
    assert provider.authorize(target_url="http://127.0.0.1:9000/challenge") == (
        "approved-for-test"
    )
    with pytest.raises(ValueError, match="localhost"):
        provider.authorize(target_url="https://www.trc.com.tw/")


def test_official_adapter_is_explicitly_not_ready() -> None:
    provider = OfficialApiVerificationProvider()
    assert provider.capabilities.ready is False
    with pytest.raises(RuntimeError, match="not configured"):
        provider.authorize(target_url="https://www.trc.com.tw/")


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="manual, mock, official_api"):
        create_verification_provider("unknown")
