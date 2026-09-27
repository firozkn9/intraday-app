"""Risk engine wired into the engine: evaluations persisted, daily-loss latch, resets."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from intraday.core.env import EnvSettings
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import (
    AiDecision,
    Alert,
    EngineCommand,
    Order,
    Position,
    RiskEvaluation,
    RiskEvent,
    Symbol,
)
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import portfolio as pf_repo
from intraday.db.session import transaction
from intraday.engine.decisions import DecisionService
from intraday.engine.risk_adapter import RiskAdapter
from intraday.engine.runtime import EngineRuntime
from intraday.marketdata.types import Quote
from tests.conftest import ADMIN_PASSWORD
from tests.integration.test_decisions import AT, CAL, seed_history

API = "/api/v1"
CFG = TradingConfig()
TODAY = date(2026, 9, 21)


async def add_symbol(sm: Any, synced: bool = True) -> None:
    async with transaction(sm) as s:
        s.add(Symbol(symbol="AAA", role="TRADABLE", asset_type="COMMON_STOCK", country="US",
                     sector="Technology", alpaca_class="us_equity", alpaca_exchange="NASDAQ",
                     alpaca_name="Aaa Inc. Common Stock", alpaca_tradable=True,
                     alpaca_status="active", alpaca_synced_at=AT if synced else None))


def quotes(spread_ask: str = "101.52", age_s: int = 3):  # type: ignore[no-untyped-def]
    async def fetch(symbols: Any) -> dict[str, Quote]:
        return {s: Quote(s, "iex", AT - timedelta(seconds=age_s), Decimal("101.48"), 1,
                         Decimal(spread_ask), 1) for s in symbols}
    return fetch


def adapter(sm: Any, fetch: Any) -> RiskAdapter:
    return RiskAdapter(sm, feed="iex", quotes=fetch, clock=lambda: AT)


async def run_cycle(sm: Any, fetch: Any, blocks: list[str]) -> None:
    svc = DecisionService(sm, feed="iex", clock=lambda: AT)
    await svc.cycle(CAL, ["AAA"], CFG, 3, blocks, risk=adapter(sm, fetch))


async def test_proposal_is_risk_evaluated_and_blocked_only_by_gate(sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    await add_symbol(sessionmaker)
    await run_cycle(sessionmaker, quotes(), ["NO_BROKER"])
    async with sessionmaker() as s:
        dec = (await s.execute(select(AiDecision))).scalar_one()
        ev = (await s.execute(select(RiskEvaluation))).scalar_one()
    assert dec.decision_result == "BLOCKED" and dec.block_reasons == ["NO_BROKER"]
    assert ev.decision_id == dec.id and ev.approved is False
    assert len(ev.checks) == 26
    failed = [c["id"] for c in ev.checks if not c["passed"]]
    assert failed == ["R1"]                       # everything but the gate passes
    # Recorded sizing: the regime multiplier (0.5 if HIGH_VOLATILITY) is applied and shown.
    assert ev.qty == ev.sizing["bounds"][ev.sizing["binding"]] > 0
    assert ev.sizing["multiplier"] in {"1", "0.5"}
    assert dec.risk_amount is not None and dec.risk_amount <= Decimal("100")
    assert ev.gate_blocks == ["NO_BROKER"]


async def test_missing_quote_and_unsynced_asset_reject(sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    await add_symbol(sessionmaker, synced=False)
    await run_cycle(sessionmaker, quotes(age_s=300), ["NO_BROKER"])
    async with sessionmaker() as s:
        dec = (await s.execute(select(AiDecision))).scalar_one()
    assert dec.decision_result == "RISK_REJECTED"
    assert dec.block_reasons == ["R7", "R26", "NO_BROKER"]


async def test_portfolio_loader(sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    async with transaction(sessionmaker) as s:
        open_id, closed_id, pend_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        s.add_all([
            Symbol(symbol="AAA", role="TRADABLE", asset_type="COMMON_STOCK", country="US",
                   sector="Technology"),
            Position(id=open_id, session_date=TODAY, symbol="AAA", status="OPEN", qty=100,
                     avg_entry_price=Decimal("100"), stop_price=Decimal("99")),
            Position(id=closed_id, session_date=TODAY, symbol="SPY", status="CLOSED", qty=0,
                     net_pnl=Decimal("-120.50"), exit_time=AT - timedelta(minutes=3)),
            Position(id=pend_id, session_date=TODAY, symbol="QQQ", status="PENDING_ENTRY",
                     qty=0, initial_stop=Decimal("479")),
        ])
    async with transaction(sessionmaker) as s:
        s.add(Order(client_order_id="ia-test-EN-1-0", symbol="QQQ", side="sell", intent="ENTRY",
                    type="limit", order_class="bracket", tif="day", qty=10,
                    limit_price=Decimal("481"), status="SUBMITTED", position_id=pend_id))
    async with sessionmaker() as s:
        pf = await pf_repo.load_portfolio(s, TODAY, "iex")
        acct = await pf_repo.load_account(s, Decimal("100000"))
    (pos,) = pf.positions
    assert pos.symbol == "AAA" and pos.mark == Decimal("101.5") and pos.sector == "Technology"
    assert pos.open_risk == Decimal("250.0")          # (101.5 - 99) x 100
    (pend,) = pf.pending
    assert pend.symbol == "QQQ" and pend.stop == Decimal("479") and pend.qty == 10
    assert pf.realized_pnl_today == Decimal("-120.50") and pf.trades_today == 3
    assert pf.last_exit_at["SPY"] == AT - timedelta(minutes=3)
    assert acct.source == "DERIVED" and acct.equity == Decimal("100000")


def runtime(env: EnvSettings, sm: Any) -> EngineRuntime:
    rt = EngineRuntime(env, sm, None, TradingConfig(), clock=lambda: AT)
    return rt


async def test_daily_loss_latch_reset_and_relatch(env: EnvSettings, sessionmaker: Any) -> None:
    rt = runtime(env, sessionmaker)
    await rt.boot()
    rt.risk = adapter(sessionmaker, quotes())
    async with transaction(sessionmaker) as s:
        s.add(Position(session_date=TODAY, symbol="AAA", status="CLOSED", qty=0,
                       net_pnl=Decimal("-520"), exit_time=AT))
    await rt.daily_loss_tick()
    assert "DAILY_LOSS_LIMIT" in rt.gate.latched
    assert rt.daily_loss is not None and rt.daily_loss["breached"] is True
    async with sessionmaker() as s:
        alert = (await s.execute(select(Alert))).scalar_one()
        event = (await s.execute(select(RiskEvent))).scalar_one()
        state = await engine_repo.get_state(s)
    assert alert.severity == "HIGH" and event.type == "DAILY_LOSS_LIMIT_REACHED"
    assert state.status == "DAILY LOSS LIMIT"

    await rt.daily_loss_tick()   # no duplicate alert while latched
    async with sessionmaker() as s:
        assert len((await s.execute(select(Alert))).scalars().all()) == 1

    async with transaction(sessionmaker) as s:
        await engine_repo.enqueue_command(s, command="START", user_id=None)
    await rt.process_commands_once()
    assert rt.trading_enabled is False            # START refused while latched

    async with transaction(sessionmaker) as s:
        await engine_repo.enqueue_command(s, command="RESET_DAILY_LOCK", user_id=None)
    await rt.process_commands_once()
    assert "DAILY_LOSS_LIMIT" not in rt.gate.blocks
    assert rt.trading_enabled is False and "PAUSED_BY_USER" in rt.gate.blocks

    await rt.daily_loss_tick()   # same session, still breached → latches again
    assert "DAILY_LOSS_LIMIT" in rt.gate.latched


async def test_risk_engine_error_latches(env: EnvSettings, sessionmaker: Any,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    await seed_history(sessionmaker)
    await add_symbol(sessionmaker)
    rt = runtime(env, sessionmaker)
    await rt.boot()
    rt.calendar = CAL
    rt.decisions = DecisionService(sessionmaker, feed="iex", clock=lambda: AT)
    rt.scanner = SimpleNamespace(passed=frozenset({"AAA"}), last=None)  # type: ignore[assignment]
    rt.risk = adapter(sessionmaker, quotes())
    rt.gate.clear("DATA_STALE")  # no market service in this test; data is in the DB

    def boom(*a: Any, **k: Any) -> None:
        raise ZeroDivisionError("synthetic failure")

    monkeypatch.setattr("intraday.risk.risk_manager.evaluate", boom)
    await rt.decision_tick()
    assert "RISK_ENGINE_ERROR" in rt.gate.latched
    async with sessionmaker() as s:
        dec = (await s.execute(select(AiDecision))).scalar_one()
    assert dec.decision_result == "RISK_REJECTED" and dec.block_reasons == ["RISK_ENGINE_ERROR"]

    async with transaction(sessionmaker) as s:
        await engine_repo.enqueue_command(s, command="RESET_LATCH", user_id=None,
                                          params={"latch": "RISK_ENGINE_ERROR"})
        await engine_repo.enqueue_command(s, command="RESET_LATCH", user_id=None,
                                          params={"latch": "DAILY_LOSS_LIMIT"})
    await rt.process_commands_once()
    assert "RISK_ENGINE_ERROR" not in rt.gate.blocks
    async with sessionmaker() as s:
        cmds = {c.params["latch"]: c.status
                for c in (await s.execute(select(EngineCommand))).scalars()}
    assert cmds == {"RISK_ENGINE_ERROR": "DONE", "DAILY_LOSS_LIMIT": "FAILED"}


async def test_reset_endpoints(auth_client: httpx.AsyncClient, sessionmaker: Any) -> None:
    body = {"confirm": "RESET", "password": ADMIN_PASSWORD}
    assert (await auth_client.post(f"{API}/controls/reset-daily-lock", json=body)
            ).status_code == 409                   # not latched
    async with transaction(sessionmaker) as s:
        await engine_repo.upsert_state(s, status="DAILY LOSS LIMIT",
                                       latched=["DAILY_LOSS_LIMIT"], trading_enabled=False)
    bad = await auth_client.post(f"{API}/controls/reset-daily-lock",
                                 json={**body, "password": "wrong-password"})
    assert bad.status_code == 403
    no_confirm = await auth_client.post(f"{API}/controls/reset-daily-lock",
                                        json={**body, "confirm": "ok"})
    assert no_confirm.status_code == 422
    ok = await auth_client.post(f"{API}/controls/reset-daily-lock", json=body)
    assert ok.status_code == 202
    bad_latch = await auth_client.post(f"{API}/controls/reset-latch",
                                       json={**body, "latch": "DAILY_LOSS_LIMIT"})
    assert bad_latch.status_code == 422           # use reset-daily-lock for that one


async def test_correlation_refresh_from_bars(sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    ra = adapter(sessionmaker, quotes())
    await ra.refresh_correlation(["AAA", "SPY", "QQQ"], CAL, 20)
    # Each seeded session has only 50 minutes of bars (~10 of 78 five-minute points):
    # too little data → every symbol is treated as insufficient (conservative).
    assert ra.correlation.insufficient == frozenset({"AAA", "SPY", "QQQ"})


async def test_decision_detail_includes_risk(auth_client: httpx.AsyncClient,
                                             sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    await add_symbol(sessionmaker)
    await run_cycle(sessionmaker, quotes(), ["NO_BROKER"])
    (d,) = (await auth_client.get(f"{API}/decisions")).json()
    detail = (await auth_client.get(f"{API}/decisions/{d['id']}")).json()
    assert detail["risk"]["qty"] > 0 and len(detail["risk"]["checks"]) == 26
    assert d["risk_amount"] is not None
