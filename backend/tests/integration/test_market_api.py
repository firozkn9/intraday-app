"""Watchlist + market API, and the engine's market-driven gate blocks."""

import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from intraday.api.context import AppContext
from intraday.core.env import EnvSettings
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import AuditLog, EngineCommand, Position, Symbol
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import marketdata as md_repo
from intraday.db.session import transaction
from intraday.engine.runtime import EngineRuntime
from intraday.marketdata.calendar import SessionDay
from intraday.marketdata.watchlist import load_seed
from tests.fakes import fixture
from tests.integration.test_market_data_service import alpaca, make_service  # noqa: F401

API = "/api/v1"
SEED = Path(__file__).resolve().parents[3] / "config" / "watchlist.seed.yaml"


async def seed_all(sm: Any) -> None:
    async with transaction(sm) as s:
        await md_repo.seed_symbols(s, [e.to_row() for e in load_seed(SEED).all],
                                   only_if_empty=True)


def stock(symbol: str, **over: Any) -> dict[str, Any]:
    return {"symbol": symbol, "role": "TRADABLE", "asset_type": "COMMON_STOCK", "country": "US",
            "sector": "Technology", "sector_etf": "XLK", **over}


# ------------------------------------------------------------------ watchlist API

async def test_list_watchlist_with_stream_budget(auth_client: httpx.AsyncClient,
                                                 sessionmaker: Any) -> None:
    await seed_all(sessionmaker)
    body = (await auth_client.get(f"{API}/watchlist")).json()
    assert len(body["symbols"]) == 31
    budget = body["stream_budget"]
    assert budget["limit"] == 30 and budget["used"] == 28 and budget["dropped"] == []
    aapl = next(s for s in body["symbols"] if s["symbol"] == "AAPL")
    # Not synced with Alpaca yet: fails closed.
    assert aapl["eligibility"]["eligible"] is False
    assert aapl["eligibility"]["rules"]["E4"]["detail"] == "Alpaca asset data not synced yet"
    spy = next(s for s in body["symbols"] if s["symbol"] == "SPY")
    assert spy["role"] == "CONTEXT" and spy["eligibility"] is None


async def test_add_tradable_stock(auth_client: httpx.AsyncClient, ctx: AppContext,
                                  fake_redis: Any) -> None:
    pubsub = fake_redis.pubsub()
    await pubsub.subscribe("watchlist:changed")
    resp = await auth_client.post(f"{API}/watchlist", json=stock("msft"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["symbol"]["symbol"] == "MSFT"
    async with ctx.sessionmaker() as s:
        assert (await s.get(Symbol, "MSFT")) is not None
        actions = list((await s.execute(select(AuditLog.action))).scalars())
    assert "WATCHLIST_ADD" in actions
    msg = None
    for _ in range(3):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        if msg:
            break
    assert msg is not None


@pytest.mark.parametrize(("body", "failed"), [
    (stock("TQQQ", asset_type="ETF"), {"E2"}),
    (stock("TSM", country="TW"), {"E3"}),
    (stock("BAC.PRL"), None),  # rejected by ticker validation (not a plain ticker)
])
async def test_non_common_stock_cannot_be_tradable(auth_client: httpx.AsyncClient,
                                                   body: dict[str, Any],
                                                   failed: set[str] | None) -> None:
    resp = await auth_client.post(f"{API}/watchlist", json=body)
    assert resp.status_code == 422
    if failed:
        assert {e["key"] for e in resp.json()["errors"]} == failed


async def test_context_etf_can_be_added(auth_client: httpx.AsyncClient) -> None:
    resp = await auth_client.post(f"{API}/watchlist", json={
        "symbol": "XLU", "role": "CONTEXT", "asset_type": "ETF", "country": "US",
        "sector": "Utilities"})
    assert resp.status_code == 201


async def test_watchlist_capacity_and_budget_warning(auth_client: httpx.AsyncClient,
                                                     sessionmaker: Any) -> None:
    await seed_all(sessionmaker)  # 17 tradable, stream 28/30
    ok = await auth_client.post(f"{API}/watchlist", json=stock("ORCL"))
    assert ok.status_code == 201 and ok.json()["warnings"] == []
    ok2 = await auth_client.post(f"{API}/watchlist", json=stock("ADBE"))
    assert ok2.json()["warnings"] == []            # 30/30
    third = await auth_client.post(f"{API}/watchlist", json=stock("CRM"))
    assert third.status_code == 201
    # The newest addition is the one left out; existing symbols keep streaming.
    assert third.json()["warnings"][0].endswith("not streamed: CRM")
    full = await auth_client.post(f"{API}/watchlist", json=stock("INTC"))
    assert full.status_code == 422 and full.json()["title"] == "Watchlist is full"


async def test_patch_revalidates_and_delete_guards_open_positions(
        auth_client: httpx.AsyncClient, sessionmaker: Any) -> None:
    await auth_client.post(f"{API}/watchlist", json=stock("AMD"))
    bad = await auth_client.patch(f"{API}/watchlist/AMD", json={"asset_type": "ETF"})
    assert bad.status_code == 422
    ok = await auth_client.patch(f"{API}/watchlist/amd", json={"enabled": False, "notes": "x"})
    assert ok.status_code == 200 and ok.json()["symbol"]["enabled"] is False

    async with transaction(sessionmaker) as s:
        s.add(Position(id=uuid.uuid4(), session_date=date(2026, 9, 21), symbol="AMD",
                       status="OPEN", qty=10))
    assert (await auth_client.delete(f"{API}/watchlist/AMD")).status_code == 409
    await auth_client.post(f"{API}/watchlist", json=stock("INTC"))
    assert (await auth_client.delete(f"{API}/watchlist/INTC")).status_code == 204
    assert (await auth_client.delete(f"{API}/watchlist/INTC")).status_code == 404


# ------------------------------------------------------------------ market API

async def test_calendar_bars_and_backfill_endpoints(auth_client: httpx.AsyncClient,
                                                    sessionmaker: Any) -> None:
    days = [SessionDay.from_alpaca(r) for r in fixture("calendar.json")]
    async with transaction(sessionmaker) as s:
        await md_repo.upsert_calendar(s, days)
    cal = (await auth_client.get(f"{API}/market/calendar",
                                 params={"start": "2026-09-01", "end": "2026-09-30"})).json()
    assert [d["date"] for d in cal["sessions"]] == ["2026-09-17", "2026-09-18", "2026-09-21",
                                                    "2026-09-22"]

    resp = await auth_client.get(f"{API}/market/bars", params={
        "symbol": "AAPL", "start": "2026-09-21T13:30:00Z", "end": "2026-09-21T14:00:00Z"})
    assert resp.status_code == 200 and resp.json()["bars"] == []
    too_big = await auth_client.get(f"{API}/market/bars", params={
        "symbol": "AAPL", "start": "2026-08-01T00:00:00Z", "end": "2026-09-21T00:00:00Z"})
    assert too_big.status_code == 422

    queued = await auth_client.post(f"{API}/market/backfill", json={"days": 5})
    assert queued.status_code == 202
    async with sessionmaker() as s:
        cmd = (await s.execute(select(EngineCommand))).scalar_one()
    assert cmd.command == "BACKFILL" and cmd.params == {"days": 5, "full": False}


async def test_status_includes_market_session(auth_client: httpx.AsyncClient,
                                              sessionmaker: Any) -> None:
    status = (await auth_client.get(f"{API}/status")).json()
    assert status["market"]["is_open"] is False and status["market"]["session_date"] is None
    assert status["market"]["data"]["enabled"] is False


# ------------------------------------------------------------------ engine integration

def runtime_with_market(env: EnvSettings, sm: Any, now: datetime) -> EngineRuntime:
    svc = make_service(sm, now)
    return EngineRuntime(env, sm, None, TradingConfig(), market_factory=lambda cfg: (svc, None),
                         clock=lambda: now)


async def test_engine_blocks_follow_calendar_and_data(env: EnvSettings, sessionmaker: Any,
                                                      alpaca: Any) -> None:  # noqa: F811
    await seed_all(sessionmaker)
    # Monday 09:40 ET: inside the entry window, but no live data yet → DATA_STALE.
    rt = runtime_with_market(env, sessionmaker, datetime(2026, 9, 21, 13, 40, tzinfo=UTC))
    await rt.boot()
    await rt.wait_background()
    assert "OUTSIDE_ENTRY_WINDOW" not in rt.gate.blocks
    assert "DATA_STALE" in rt.gate.blocks
    assert "stream disconnected" in rt.gate.details["DATA_STALE"]
    async with sessionmaker() as s:
        st = await engine_repo.get_state(s)
    assert st.details["market"]["is_open"] is True
    assert st.details["market"]["window"]["trading_start"] == "2026-09-21T13:35:00+00:00"
    assert st.details["market_data"]["enabled"] is True

    # Saturday: calendar says closed → outside window; no staleness outside the session.
    rt2 = runtime_with_market(env, sessionmaker, datetime(2026, 9, 19, 15, 0, tzinfo=UTC))
    await rt2.boot()
    await rt2.wait_background()
    assert "OUTSIDE_ENTRY_WINDOW" in rt2.gate.blocks and "DATA_STALE" not in rt2.gate.blocks
    assert "Mon 2026-09-21 09:35 ET" in rt2.gate.details["OUTSIDE_ENTRY_WINDOW"]


async def test_backfill_command_runs_in_background(env: EnvSettings, sessionmaker: Any,
                                                   alpaca: Any) -> None:  # noqa: F811
    await seed_all(sessionmaker)
    rt = runtime_with_market(env, sessionmaker, datetime(2026, 9, 21, 13, 40, tzinfo=UTC))
    await rt.boot()
    await rt.wait_background()
    async with transaction(sessionmaker) as s:
        cmd = await engine_repo.enqueue_command(s, command="BACKFILL", user_id=None,
                                                params={"days": 2, "full": True})
    await rt.process_commands_once()
    await rt.wait_background()
    async with sessionmaker() as s:
        done = await s.get(EngineCommand, cmd.id)
    assert done.status == "DONE", done.result
    assert done.result["minute_bars"] > 0 and done.result["error"] is None


@respx.mock
async def test_calendar_sync_failure_falls_back_to_db(env: EnvSettings, sessionmaker: Any) -> None:
    days = [SessionDay.from_alpaca(r) for r in fixture("calendar.json")]
    async with transaction(sessionmaker) as s:
        await md_repo.upsert_calendar(s, days)
    respx.get("https://paper-api.alpaca.markets/v2/calendar").mock(
        return_value=httpx.Response(500))
    respx.get(url__regex=r"https://paper-api\.alpaca\.markets/v2/assets/.*").mock(
        return_value=httpx.Response(500))
    rt = runtime_with_market(env, sessionmaker, datetime(2026, 9, 21, 13, 40, tzinfo=UTC))
    await rt.boot()  # must not raise
    await rt.wait_background()
    assert rt.calendar.session(date(2026, 9, 21)) is not None
