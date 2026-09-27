"""Migrations up/down, DB-level safety constraints, and role grants (docs/design/04)."""

import asyncio
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from intraday.db.base import Base
from tests.conftest import alembic_config, create_database, drop_database


@pytest.fixture
def scratch_db(pg_server_url: str) -> Iterator[str]:
    url = create_database(pg_server_url)
    yield url
    drop_database(pg_server_url, url)


async def _tables(url: str) -> set[str]:
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {r["tablename"] for r in rows}
    finally:
        await conn.close()


def test_upgrade_downgrade_upgrade(scratch_db: str) -> None:
    cfg = alembic_config(scratch_db)
    command.upgrade(cfg, "head")
    assert set(Base.metadata.tables) <= asyncio.run(_tables(scratch_db))
    command.downgrade(cfg, "base")
    assert asyncio.run(_tables(scratch_db)) == {"alembic_version"}
    command.upgrade(cfg, "head")
    assert set(Base.metadata.tables) <= asyncio.run(_tables(scratch_db))


async def test_models_match_migrations(db_engine: AsyncEngine) -> None:
    """Autogenerate against head must find nothing to do (no model/migration drift)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    def compare(sync_conn):  # type: ignore[no-untyped-def]
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)

    async with db_engine.connect() as c:
        diff = await c.run_sync(compare)
    assert diff == []


@pytest_asyncio.fixture(loop_scope="session")
async def conn(db_engine: AsyncEngine) -> AsyncIterator[object]:
    async with db_engine.connect() as c:
        trans = await c.begin()
        yield c
        await trans.rollback()


async def _insert_symbol(c, symbol: str, role: str, asset_type: str, country: str) -> None:  # type: ignore[no-untyped-def]
    await c.execute(text(
        "INSERT INTO symbols (symbol, role, asset_type, country) VALUES (:s, :r, :a, :c)"),
        {"s": symbol, "r": role, "a": asset_type, "c": country})


async def _insert_buy(c, symbol: str) -> None:  # type: ignore[no-untyped-def]
    await c.execute(text(
        "INSERT INTO orders (id, client_order_id, symbol, side, intent, type, order_class, tif, "
        "qty, status) VALUES (gen_random_uuid(), :coid, :s, 'buy', 'ENTRY', 'limit', "
        "'bracket', 'day', 1, 'PENDING_SUBMIT')"), {"coid": f"test-{symbol}", "s": symbol})


async def test_tradable_symbols_must_be_us_common_stock(conn) -> None:  # type: ignore[no-untyped-def]
    for asset_type, country in (("ETF", "US"), ("COMMON_STOCK", "TW"), ("OTHER", "US")):
        nested = await conn.begin_nested()
        with pytest.raises(IntegrityError, match="tradable_is_us_common_stock"):
            await _insert_symbol(conn, "BAD", "TRADABLE", asset_type, country)
        await nested.rollback()
    await _insert_symbol(conn, "SPY", "CONTEXT", "ETF", "US")
    await _insert_symbol(conn, "AAPL", "TRADABLE", "COMMON_STOCK", "US")


async def test_buy_orders_only_for_tradable_symbols(conn) -> None:  # type: ignore[no-untyped-def]
    await _insert_symbol(conn, "SPY", "CONTEXT", "ETF", "US")
    await _insert_symbol(conn, "AAPL", "TRADABLE", "COMMON_STOCK", "US")
    await _insert_buy(conn, "AAPL")
    for sym in ("SPY", "UNKNOWN"):
        nested = await conn.begin_nested()
        with pytest.raises(DBAPIError, match=r"not a tradable U.S. common stock"):
            await _insert_buy(conn, sym)
        await nested.rollback()


async def test_engine_role_cannot_rewrite_audit_tables(conn) -> None:  # type: ignore[no-untyped-def]
    await conn.execute(text("SET LOCAL ROLE intraday_engine_role"))
    await conn.execute(text(
        "INSERT INTO system_events (id, component, type, severity, message) "
        "VALUES (gen_random_uuid(), 'test', 'T', 'INFO', 'ok')"))
    for stmt in ("UPDATE system_events SET message = 'x'", "DELETE FROM system_events",
                 "UPDATE fills SET qty = 1", "DELETE FROM audit_log"):
        nested = await conn.begin_nested()
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(text(stmt))
        await nested.rollback()


async def test_api_role_cannot_touch_trading_tables(conn) -> None:  # type: ignore[no-untyped-def]
    await conn.execute(text("SET LOCAL ROLE intraday_api_role"))
    for stmt in ("UPDATE orders SET qty = 1", "DELETE FROM positions",
                 "UPDATE engine_state SET trading_enabled = true",
                 "UPDATE engine_commands SET status = 'DONE'"):
        nested = await conn.begin_nested()
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(text(stmt))
        await nested.rollback()


async def test_engine_can_prune_via_security_definer(conn) -> None:  # type: ignore[no-untyped-def]
    await conn.execute(text("SET LOCAL ROLE intraday_engine_role"))
    result = (await conn.execute(text("SELECT prune_retention()"))).scalar_one()
    assert set(result) == {"trading_signals", "system_events", "universe_scans", "sessions"}
