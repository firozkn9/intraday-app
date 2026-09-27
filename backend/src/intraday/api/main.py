"""FastAPI application (api container). Holds no broker credentials."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from redis.asyncio import Redis

from intraday import __version__
from intraday.api.context import AppContext
from intraday.api.deps import CurrentPrincipal
from intraday.api.errors import install_error_handlers
from intraday.api.routers import (
    auth,
    backtests,
    decisions,
    market,
    positions,
    reports,
    scanner,
    settings,
    status,
    system,
)
from intraday.api.security.rate_limit import FallbackRateLimiter
from intraday.api.security.tokens import TokenHasher
from intraday.backtest.job import run_in_process
from intraday.core.env import EnvSettings, get_env, is_placeholder
from intraday.core.logging import configure_logging, get_logger
from intraday.core.runtime_config import load_defaults
from intraday.db.repositories import marketdata as md_repo
from intraday.db.repositories import settings as settings_repo
from intraday.db.session import make_engine, make_sessionmaker, transaction
from intraday.marketdata.watchlist import load_seed

API_PREFIX = "/api/v1"
MIN_SESSION_SECRET_LENGTH = 32

log = get_logger(__name__)


def build_context(env: EnvSettings) -> AppContext:
    secret = env.session_secret.get_secret_value()
    if len(secret) < MIN_SESSION_SECRET_LENGTH or is_placeholder(secret):
        raise RuntimeError(f"SESSION_SECRET must be a random value of at least "
                           f"{MIN_SESSION_SECRET_LENGTH} chars (not the .env.example placeholder)")
    engine = make_engine(env.database_url)
    # REDIS_URL=none runs without Redis (polling and in-memory rate limits).
    redis = (None if env.redis_url.lower() in ("", "none") else
             Redis.from_url(env.redis_url, socket_timeout=2, socket_connect_timeout=2))
    return AppContext(env=env, sessionmaker=make_sessionmaker(engine), redis=redis,
                      limiter=FallbackRateLimiter(redis), tokens=TokenHasher(secret.encode()),
                      defaults=load_defaults(env.defaults_path),
                      launch_backtest=process_launcher(env.database_url))


_EXECUTOR: ProcessPoolExecutor | None = None


def process_launcher(db_url: str) -> Callable[[uuid.UUID], None]:
    """One worker process, one backtest at a time, off the API's event loop."""
    def launch(run_id: uuid.UUID) -> None:
        global _EXECUTOR
        if _EXECUTOR is None:
            _EXECUTOR = ProcessPoolExecutor(max_workers=1)
        _EXECUTOR.submit(run_in_process, db_url, str(run_id))
    return launch


def create_app(ctx: AppContext | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "ctx", None) is None:
            env = get_env()
            configure_logging(env.log_level)
            app.state.ctx = build_context(env)
        context: AppContext = app.state.ctx
        try:
            async with transaction(context.sessionmaker) as s:
                await settings_repo.ensure_initial(s, context.defaults)
                seed = load_seed(context.env.watchlist_seed_path)
                added = await md_repo.seed_symbols(s, [e.to_row() for e in seed.all],
                                                   only_if_empty=True)
                if added:
                    log.info("watchlist_seeded", symbols=added)
        except Exception as exc:
            log.warning("startup_init_failed", error=str(exc))
        yield
        if context.redis is not None:
            await context.redis.aclose()

    app = FastAPI(title="Intraday App API", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    install_error_handlers(app)

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(status.health_router)
    api.include_router(auth.router)
    api.include_router(settings.router)
    api.include_router(status.router)
    api.include_router(market.router)
    api.include_router(scanner.router)
    api.include_router(decisions.router)
    api.include_router(positions.router)
    api.include_router(reports.router)
    api.include_router(system.router)
    api.include_router(system.internal_router)
    api.include_router(backtests.router)

    @api.get("/openapi.json", include_in_schema=False)
    async def openapi(principal: CurrentPrincipal) -> dict[str, Any]:
        return app.openapi()

    app.include_router(api)
    return app


app = create_app()
