"""Session and CSRF tokens. Only HMAC digests are stored server-side."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

SESSION_COOKIE = "sid"
CSRF_COOKIE = "csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_IDLE_TIMEOUT = timedelta(hours=12)
SESSION_ABSOLUTE_TIMEOUT = timedelta(days=7)


@dataclass(frozen=True)
class TokenHasher:
    secret: bytes

    def digest(self, token: str) -> str:
        return hmac.new(self.secret, token.encode(), hashlib.sha256).hexdigest()

    def matches(self, token: str | None, expected_digest: str) -> bool:
        if not token:
            return False
        return hmac.compare_digest(self.digest(token), expected_digest)


def new_token() -> str:
    return secrets.token_urlsafe(32)
