import pytest

from tra_sniper.auth import TokenManager, hash_password, verify_password


def test_password_hash_round_trip() -> None:
    encoded = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", encoded)
    assert not verify_password("incorrect-password", encoded)
    assert "correct-horse" not in encoded


def test_short_password_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 10"):
        hash_password("short")


def test_signed_token_expires() -> None:
    manager = TokenManager("x" * 32, ttl_seconds=60)
    token = manager.issue(42, now=100)
    assert manager.verify(token, now=159).user_id == 42
    with pytest.raises(ValueError, match="invalid or expired"):
        manager.verify(token, now=160)
