"""Engine process entrypoint: ``python -m intraday.engine.main``."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from redis.asyncio import Redis
from sqlalchemy.exc import SQLAlchemyError

from intraday.core.env import get_env
from intraday.core.logging import configure_logging, get_logger
from intraday.core.runtime_config import load_defaults
from intraday.db.session import make_engine, make_sessionmaker
from intraday.engine.runtime import EngineRuntime
from intraday.marketdata.factory import build_market_data
from intraday.trading.broker.factory import build_broker

log = get_logger(__name__)


async def _main() -> None:
    env = get_env()
    configure_logging(env.log_level)
    engine = make_engine(env.database_url)
    redis = (None if env.redis_url.lower() in ("", "none") else
             Redis.from_url(env.redis_url, socket_timeout=2, socket_connect_timeout=2))
    sessionmaker = make_sessionmaker(engine)
    runtime = EngineRuntime(
        env, sessionmaker, redis, load_defaults(env.defaults_path),
        market_factory=lambda cfg: build_market_data(env, sessionmaker, feed=cfg.data.feed),
        broker_factory=lambda: build_broker(env),
        enable_scheduler=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # not supported on Windows event loops
            loop.add_signal_handler(sig, stop.set)

    backoff = 1.0
    while not stop.is_set():  # wait for the database on first start
        try:
            await runtime.run(stop)
            break
        except (SQLAlchemyError, OSError) as exc:
            log.error("engine_boot_failed", error=str(exc), retry_in=backoff)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 30.0)

    if redis is not None:
        await redis.aclose()
    await engine.dispose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())


if __name__ == "__main__":
    main()
