"""Start a full local stack for the Playwright E2E suite (Phase 15). No network, no keys.

    cd backend && uv run python scripts/e2e_stack.py        # Playwright starts this itself

1. PostgreSQL: $E2E_DATABASE_SERVER (postgresql://user:pass@host:port/postgres) or an
   embedded pgserver; a fresh ``intraday_e2e`` database is created and migrated.
2. Seeds settings, the admin (admin@example.com / $E2E_ADMIN_PASSWORD), the synthetic
   demo session, and runs the engine replay so trades exist (calendar, history, CSV).
3. Latches DAILY_LOSS_LIMIT in engine_state (restored at engine boot) so the suite can
   exercise the lock-and-reset flow.
4. Runs the API (port 8000) and the engine with ENGINE_BROKER=sim + ENGINE_DATA=replay
   until interrupted. The developer's .env is never read (INTRADAY_SKIP_ENV_FILES=1).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import asyncpg
from alembic import command
from alembic.config import Config

BACKEND = Path(__file__).resolve().parents[1]
DB_NAME = "intraday_e2e"
DAY = date(2026, 9, 21)
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "correct-horse-battery-staple")

os.environ["INTRADAY_SKIP_ENV_FILES"] = "1"


def server_url() -> str:
    url = os.environ.get("E2E_DATABASE_SERVER")
    if url:
        return url
    import pgserver

    srv = pgserver.get_server(tempfile.mkdtemp(prefix="intraday-e2e-pg-"), cleanup_mode="stop")
    return str(srv.get_uri())


async def recreate(url: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        await conn.close()


async def seed(db_url: str) -> None:
    from intraday.api.security import passwords
    from intraday.backtest.job import load_calendar
    from intraday.core.runtime_config import load_defaults
    from intraday.db.repositories import engine as engine_repo
    from intraday.db.repositories import settings as settings_repo
    from intraday.db.repositories import users as users_repo
    from intraday.db.session import make_engine, make_sessionmaker, transaction
    from intraday.engine.demo import seed_demo
    from intraday.engine.replay import replay_session

    engine = make_engine(db_url, pool_size=3)
    sm = make_sessionmaker(engine)
    try:
        defaults = load_defaults(BACKEND.parent / "config" / "defaults.yaml")
        async with transaction(sm) as s:
            active = await settings_repo.ensure_initial(s, defaults)
            user = await users_repo.create_user(s, ADMIN_EMAIL,
                                                passwords.hash_password(ADMIN_PASSWORD))
            await users_repo.audit(s, action="ADMIN_CREATED", user_id=user.id, target=ADMIN_EMAIL)
        await seed_demo(sm, DAY)
        cal = await load_calendar(sm, DAY - timedelta(days=40), DAY)
        report = await replay_session(sm, cal, DAY, defaults, active.version, ["AAA"])
        print(f"e2e replay: {len(report.trades)} trades, net {report.realized_net}", flush=True)
        async with transaction(sm) as s:
            await engine_repo.upsert_state(
                s, status="DAILY LOSS LIMIT", trading_enabled=False,
                blocks=[{"reason": "DAILY_LOSS_LIMIT", "since": None, "latched": True,
                         "detail": "E2E: daily loss limit reached"}],
                latched=["DAILY_LOSS_LIMIT"], details={})
    finally:
        await engine.dispose()


def main() -> None:
    base = server_url().replace("postgresql+asyncpg://", "postgresql://")
    asyncio.run(recreate(base))
    db_url = base.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://", 1) \
        + f"/{DB_NAME}"
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.attributes["url"] = db_url
    command.upgrade(cfg, "head")
    asyncio.run(seed(db_url))

    env = {**os.environ, "DATABASE_URL": db_url, "TRADING_MODE": "PAPER",
           "ALPACA_BASE_URL": "https://paper-api.alpaca.markets", "ALPACA_API_KEY_ID": "",
           "ALPACA_API_SECRET_KEY": "", "ANTHROPIC_API_KEY": "", "ENGINE_BROKER": "sim",
           "ENGINE_DATA": "replay", "SESSION_SECRET": "e2e-" + "x" * 60,
           "PUBLIC_ORIGIN": os.environ.get("E2E_ORIGIN", "http://localhost:5173"),
           "COOKIE_SECURE": "false", "REDIS_URL": "none",
           "LOG_LEVEL": "WARNING", "INTRADAY_SKIP_ENV_FILES": "1"}
    procs = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "intraday.api.main:app",
                          "--host", "127.0.0.1", "--port", "8000"], env=env, cwd=BACKEND),
        subprocess.Popen([sys.executable, "-m", "intraday.engine.main"], env=env, cwd=BACKEND),
    ]
    print("e2e stack running: api http://127.0.0.1:8000", flush=True)

    def stop(*_: object) -> None:
        for p in procs:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while all(p.poll() is None for p in procs):
        time.sleep(0.5)
    stop()


if __name__ == "__main__":
    main()
