"""Alembic environment (async, asyncpg).

Migrations run as the schema owner: DATABASE_URL_OWNER, falling back to
DATABASE_URL. The api/engine login roles never run migrations.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from intraday.core.env import get_env
from intraday.db import models  # noqa: F401  (register tables)
from intraday.db.base import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # Same sources as the app: environment, then .env.local / .env (see intraday.core.env).
    env = get_env()
    url = config.attributes.get("url") or env.database_url_owner \
        or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL_OWNER (or DATABASE_URL) to run migrations")
    return str(url)


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # programmatic use (tests)
        _run(connection)
    else:
        asyncio.run(_run_async())


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
