"""Sliding-window rate limiting: Redis when available, in-memory fallback."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from intraday.core.ids import token_urlsafe
from intraday.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Limit:
    name: str
    max_requests: int
    window_seconds: int


LOGIN_PER_IP = Limit("login_ip", 5, 60)
LOGIN_PER_ACCOUNT = Limit("login_account", 20, 3600)
MUTATIONS = Limit("mutations", 30, 60)
READS = Limit("reads", 300, 60)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int


class RateLimiter(Protocol):
    async def hit(self, limit: Limit, key: str) -> Decision: ...


class MemoryRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _now(self) -> float:
        return time.monotonic()

    async def hit(self, limit: Limit, key: str) -> Decision:
        now = self._now()
        q = self._hits[f"{limit.name}:{key}"]
        while q and q[0] <= now - limit.window_seconds:
            q.popleft()
        if len(q) >= limit.max_requests:
            return Decision(False, max(1, int(q[0] + limit.window_seconds - now) + 1))
        q.append(now)
        return Decision(True, 0)

    def reset(self) -> None:
        self._hits.clear()


class RedisRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, limit: Limit, key: str) -> Decision:
        rkey = f"rl:{limit.name}:{key}"
        now = time.time()
        window_start = now - limit.window_seconds
        pipe = self._redis.pipeline(transaction=True)
        pipe.zremrangebyscore(rkey, 0, window_start)
        pipe.zcard(rkey)
        pipe.zrange(rkey, 0, 0, withscores=True)
        _, count, oldest = await pipe.execute()
        if int(count) >= limit.max_requests:
            oldest_ts = float(oldest[0][1]) if oldest else now
            return Decision(False, max(1, int(oldest_ts + limit.window_seconds - now) + 1))
        pipe = self._redis.pipeline(transaction=True)
        pipe.zadd(rkey, {f"{now}:{token_urlsafe(6)}": now})
        pipe.expire(rkey, limit.window_seconds + 1)
        await pipe.execute()
        return Decision(True, 0)


class FallbackRateLimiter:
    """Use Redis; if Redis errors, fall back to per-process memory limits."""

    def __init__(self, redis: Redis | None) -> None:
        self._redis = RedisRateLimiter(redis) if redis is not None else None
        self._memory = MemoryRateLimiter()

    async def hit(self, limit: Limit, key: str) -> Decision:
        if self._redis is not None:
            try:
                return await self._redis.hit(limit, key)
            except (RedisError, OSError) as exc:
                log.warning("rate_limit_redis_unavailable", error=str(exc))
        return await self._memory.hit(limit, key)
