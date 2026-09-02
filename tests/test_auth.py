import pytest

from tra_sniper.auth import TokenManager, hash_password, verify_password


def test_password_hash_round_trip() -> None:
    encoded = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", encoded)
    assert not verify_password("incorrect-password", encoded)
    assert "correct-horse" not in encoded


def test_short_password_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 12"):
        hash_password("short")


def test_signed_token_expires() -> None:
    manager = TokenManager("x" * 32, ttl_seconds=60)
    token = manager.issue(42, 7, now=100)
    claims = manager.verify(token, now=159)
    assert claims.user_id == 42
    assert claims.version == 7
    with pytest.raises(ValueError, match="invalid or expired"):
        manager.verify(token, now=160)
