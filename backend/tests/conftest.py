"""Shared fixtures.

Database: a real PostgreSQL. Uses TEST_DATABASE_URL (a server where we may
CREATE DATABASE, e.g. the CI service container) or, if unset, an embedded
server via ``pgserver``. Each test session gets a fresh database migrated to
head; tables are truncated after every test.

No test talks to Alpaca or Anthropic (CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import fakeredis
import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from intraday.api.context import AppContext
from intraday.api.main import create_app
from intraday.api.security import passwords
from intraday.api.security.rate_limit import MemoryRateLimiter
from intraday.api.security.tokens import CSRF_COOKIE, CSRF_HEADER, TokenHasher
from intraday.core.env import EnvSettings
from intraday.core.runtime_config import load_defaults
from intraday.db.repositories import users as users_repo
from intraday.db.session import make_engine, make_sessionmaker, transaction

# Never read a developer's .env / .env.local in tests (get_env() is only called lazily).
os.environ["INTRADAY_SKIP_ENV_FILES"] = "1"

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
ORIGIN = "http://testserver"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "correct-horse-battery-staple"


def _asyncpg_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _sqlalchemy_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="session")
def pg_server_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield _asyncpg_url(url)
        return
    import pgserver

    server = pgserver.get_server(tempfile.mkdtemp(prefix="intraday-pg-"), cleanup_mode="stop")
    yield server.get_uri()


def _with_db(server_url: str, db: str) -> str:
    base, _, _ = server_url.rpartition("/")
    return f"{base}/{db}"


def create_database(server_url: str) -> str:
    name = f"intraday_test_{uuid.uuid4().hex[:10]}"

    async def run() -> None:
        conn = await asyncpg.connect(server_url)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    asyncio.run(run())
    return _sqlalchemy_url(_with_db(server_url, name))


def drop_database(server_url: str, db_url: str) -> None:
    name = db_url.rsplit("/", 1)[1]

    async def run() -> None:
        conn = await asyncpg.connect(server_url)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()

    asyncio.run(run())


def alembic_config(db_url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.attributes["url"] = db_url
    return cfg


@pytest.fixture(scope="session")
def db_url(pg_server_url: str) -> Iterator[str]:
    url = create_database(pg_server_url)
    command.upgrade(alembic_config(url), "head")
    yield url
    drop_database(pg_server_url, url)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def sessionmaker(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(db_engine)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean_tables(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    uses_db = "db_engine" in request.fixturenames
    engine: AsyncEngine | None = request.getfixturevalue("db_engine") if uses_db else None
    yield
    if engine is None:
        return
    async with engine.begin() as conn:
        tables = (await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"))).scalars().all()
        await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


@pytest.fixture
def env(db_url: str) -> EnvSettings:
    return EnvSettings.model_validate({
        "TRADING_MODE": "PAPER",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
        "DATABASE_URL": db_url,
        "SESSION_SECRET": "x" * 48,
        "PUBLIC_ORIGIN": ORIGIN,
        "COOKIE_SECURE": False,
        "INTRADAY_DEFAULTS_PATH": str(REPO / "config" / "defaults.yaml"),
    })


@pytest.fixture
def fake_redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
def ctx(env: EnvSettings, sessionmaker: async_sessionmaker[AsyncSession],
        fake_redis: fakeredis.FakeAsyncRedis) -> AppContext:
    return AppContext(env=env, sessionmaker=sessionmaker, redis=fake_redis,
                      limiter=MemoryRateLimiter(),
                      tokens=TokenHasher(env.session_secret.get_secret_value().encode()),
                      defaults=load_defaults(env.defaults_path))


@pytest_asyncio.fixture(loop_scope="session")
async def client(ctx: AppContext) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN,
                                 headers={"Origin": ORIGIN}) as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def admin(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    async with transaction(sessionmaker) as s:
        user = await users_repo.create_user(s, ADMIN_EMAIL,
                                            passwords.hash_password(ADMIN_PASSWORD))
    return {"id": user.id, "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}


async def login(client: httpx.AsyncClient, email: str = ADMIN_EMAIL,
                password: str = ADMIN_PASSWORD) -> httpx.Response:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if resp.status_code == 200:
        client.headers[CSRF_HEADER] = client.cookies[CSRF_COOKIE]
    return resp


@pytest_asyncio.fixture(loop_scope="session")
async def auth_client(client: httpx.AsyncClient, admin: dict[str, Any]
                      ) -> httpx.AsyncClient:
    resp = await login(client)
    assert resp.status_code == 200, resp.text
    return client
