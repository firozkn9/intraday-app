"""Per-application dependencies, built once at startup (or injected by tests)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.api.security.rate_limit import RateLimiter
from intraday.api.security.tokens import TokenHasher
from intraday.core.env import EnvSettings
from intraday.core.logging import get_logger
from intraday.core.runtime_config import TradingConfig

log = get_logger(__name__)


@dataclass
class AppContext:
    env: EnvSettings
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis | None
    limiter: RateLimiter
    tokens: TokenHasher
    defaults: TradingConfig
    # Starts a queued backtest outside the API's event loop (a worker process in prod).
    launch_backtest: Callable[[uuid.UUID], None] | None = None

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        """Best-effort notify; the engine also polls the DB, so failures are non-fatal."""
        if self.redis is None:
            return
        try:
            await self.redis.publish(channel, json.dumps(message, default=str))
        except (RedisError, OSError) as exc:
            log.warning("redis_publish_failed", channel=channel, error=str(exc))
