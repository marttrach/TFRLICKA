from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("password must contain at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt$16384$8$1${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(actual, _b64decode(expected))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: int
    expires_at: int


class TokenManager:
    def __init__(self, secret: str | None = None, *, ttl_seconds: int = 86_400) -> None:
        configured = secret or os.getenv("TRA_TOKEN_SECRET")
        if not configured:
            configured = secrets.token_urlsafe(48)
        if len(configured) < 32:
            raise ValueError("TRA_TOKEN_SECRET must contain at least 32 characters")
        self._secret = configured.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    def issue(self, user_id: int, *, now: int | None = None) -> str:
        issued_at = int(time.time()) if now is None else now
        payload = json.dumps(
            {"sub": user_id, "exp": issued_at + self.ttl_seconds},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded = _b64encode(payload)
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded}.{_b64encode(signature)}"

    def verify(self, token: str, *, now: int | None = None) -> TokenClaims:
        try:
            encoded, signature = token.split(".", 1)
            expected = hmac.new(
                self._secret,
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(expected, _b64decode(signature)):
                raise ValueError("invalid token signature")
            payload = json.loads(_b64decode(encoded))
            expires_at = int(payload["exp"])
            current = int(time.time()) if now is None else now
            if expires_at <= current:
                raise ValueError("token expired")
            return TokenClaims(user_id=int(payload["sub"]), expires_at=expires_at)
        except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid or expired token") from exc
