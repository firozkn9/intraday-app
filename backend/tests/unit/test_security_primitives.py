"""Passwords, tokens, rate limiting, ids."""

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from intraday.api.security import passwords
from intraday.api.security.rate_limit import (
    FallbackRateLimiter,
    Limit,
    MemoryRateLimiter,
    RedisRateLimiter,
)
from intraday.api.security.tokens import TokenHasher, new_token
from intraday.core.ids import uuid7

LIMIT = Limit("t", 3, 60)


def test_argon2id_hash_and_verify() -> None:
    h = passwords.hash_password("a-long-enough-password")
    assert h.startswith("$argon2id$")
    assert passwords.verify_password(h, "a-long-enough-password")
    assert not passwords.verify_password(h, "wrong-password-here")
    assert not passwords.verify_password(None, "anything-at-all")
    assert not passwords.verify_password("not-a-hash", "anything-at-all")


@pytest.mark.parametrize("pw", ["short", "a" * 20, "admin@example.com", "x" * 300])
def test_weak_passwords_rejected(pw: str) -> None:
    with pytest.raises(passwords.WeakPassword):
        passwords.check_policy(pw, email="admin@example.com")


def test_token_hasher_constant_time_match() -> None:
    hasher = TokenHasher(b"s" * 32)
    token = new_token()
    assert hasher.matches(token, hasher.digest(token))
    assert not hasher.matches(token + "x", hasher.digest(token))
    assert not hasher.matches(None, hasher.digest(token))
    assert hasher.digest(token) != TokenHasher(b"t" * 32).digest(token)


async def test_memory_limiter_window(monkeypatch: pytest.MonkeyPatch) -> None:
    lim = MemoryRateLimiter()
    now = [1000.0]
    monkeypatch.setattr(lim, "_now", lambda: now[0])
    assert all([(await lim.hit(LIMIT, "ip")).allowed for _ in range(3)])
    blocked = await lim.hit(LIMIT, "ip")
    assert not blocked.allowed and 1 <= blocked.retry_after <= 61
    assert (await lim.hit(LIMIT, "other-ip")).allowed
    now[0] += 61
    assert (await lim.hit(LIMIT, "ip")).allowed


async def test_redis_limiter() -> None:
    lim = RedisRateLimiter(fakeredis.FakeAsyncRedis())
    assert all([(await lim.hit(LIMIT, "k")).allowed for _ in range(3)])
    assert not (await lim.hit(LIMIT, "k")).allowed


async def test_fallback_limiter_survives_redis_outage() -> None:
    class Broken:
        def pipeline(self, *a: object, **k: object) -> object:
            raise RedisConnectionError("down")

    lim = FallbackRateLimiter(Broken())  # type: ignore[arg-type]
    assert all([(await lim.hit(LIMIT, "k")).allowed for _ in range(3)])
    assert not (await lim.hit(LIMIT, "k")).allowed


def test_uuid7_is_versioned_and_time_ordered() -> None:
    ids = [uuid7() for _ in range(50)]
    assert all(u.version == 7 and u.variant == "specified in RFC 4122" for u in ids)
    stamps = [u.int >> 80 for u in ids]
    assert stamps == sorted(stamps)
