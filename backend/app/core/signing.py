"""HMAC-SHA256 signatures for short-lived capabilities (local upload URLs, status tokens).

Every signature is bound to a ``purpose`` so a value signed for one use cannot be replayed for
another.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from app.core.config import get_settings


def sign(purpose: str, *parts: str | int) -> str:
    message = "\x1f".join([purpose, *(str(part) for part in parts)]).encode()
    key = get_settings().APP_SECRET_KEY.get_secret_value().encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify(signature: str, purpose: str, *parts: str | int) -> bool:
    return hmac.compare_digest(signature, sign(purpose, *parts))


def expiry(ttl_seconds: int) -> int:
    return int(time.time()) + ttl_seconds


def is_expired(expires_at: int) -> bool:
    return expires_at < int(time.time())
