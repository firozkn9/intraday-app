"""Decision loop against the database: contexts, cycles, persistence, API, offline replay."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from intraday.core import env as env_module
from intraday.core.env import EnvSettings
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import AiDecision, TradingSignal
from intraday.db.repositories import marketdata as md_repo
from intraday.db.session import transaction
from intraday.engine.decisions import DecisionService, last_completed_bar, load_contexts
from intraday.engine.runtime import EngineRuntime
from intraday.marketdata.calendar import MarketCalendar, SessionDay
from tests.session_fixtures import SESSION, bars, breakout_day, breakout_volumes, uptrend

API = "/api/v1"
CFG = TradingConfig()
PRIOR_DATES = [date(2026, 9, 2) + timedelta(days=i) for i in range(17)
               if (date(2026, 9, 2) + timedelta(days=i)).weekday() < 5][-12:]


def session_for(d: date) -> SessionDay:
    return SessionDay.from_alpaca({"date": d.isoformat(), "open": "09:30", "close": "16:00"})


PRIORS = [session_for(d) for d in PRIOR_DATES]
CAL = MarketCalendar([*PRIORS, SESSION])
N = len(breakout_day())
AT = SESSION.open_at + timedelta(minutes=N, seconds=6)   # just after the breakout bar closed


async def seed_history(sm: Any) -> None:
    rows = []
    for sess in PRIORS:
        for sym, start in (("AAA", 100.0), ("SPY", 570.0), ("QQQ", 480.0)):
            closes = [start + 0.02 * i + (0.03 if i % 2 else -0.03) for i in range(50)]
            rows += bars(sym, closes, 500.0, session=sess)
    for sym, closes, vols in (("AAA", breakout_day(), breakout_volumes()),
                              ("SPY", uptrend(N, 570.0, 0.05), [1000.0] * N),
                              ("QQQ", uptrend(N, 480.0, 0.05), [1000.0] * N)):
        rows += bars(sym, closes, vols)
    async with transaction(sm) as s:
        await md_repo.upsert_bars(s, rows)
        await md_repo.upsert_calendar(s, [*PRIORS, SESSION])


def test_last_completed_bar() -> None:
    assert last_completed_bar(SESSION.open_at + timedelta(seconds=30), SESSION) is None
    assert last_completed_bar(SESSION.open_at + timedelta(minutes=1, seconds=4), SESSION) is None
    assert last_completed_bar(SESSION.open_at + timedelta(minutes=1, seconds=6),
                              SESSION) == SESSION.open_at
    late = SESSION.close_at + timedelta(minutes=5)
    assert last_completed_bar(late, SESSION) == SESSION.close_at - timedelta(minutes=1)


async def test_contexts_use_same_feed_history(sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    ctx = await load_contexts(sessionmaker, CAL, "iex", SESSION, ["AAA", "SPY"])
    aaa = ctx["AAA"]
    assert aaa.volume_profile is not None and aaa.volume_profile[9] == pytest.approx(5000)
    assert len(aaa.warmup) == 50 and aaa.warmup[0].ts.date() == PRIOR_DATES[-1]
    assert len(aaa.rv30_history) == 12  # one 30-bar window per prior session
    # Another feed has no history → no volume profile (RVOL unavailable, never mixed).
    sip = await load_contexts(sessionmaker, CAL, "sip", SESSION, ["AAA"])
    assert sip["AAA"].volume_profile is None


async def test_cycle_persists_signals_and_blocked_proposals(sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    svc = DecisionService(sessionmaker, feed="iex", clock=lambda: AT)
    result = await svc.cycle(CAL, ["AAA"], CFG, 3, ["NO_BROKER", "PAUSED_BY_USER"])
    assert result is not None and result.bar_ts == SESSION.open_at + timedelta(minutes=N - 1)
    assert [p.symbol for p in result.proposals] == ["AAA"]

    async with sessionmaker() as s:
        sigs = (await s.execute(select(TradingSignal))).scalars().all()
        decs = (await s.execute(select(AiDecision))).scalars().all()
    assert {x.strategy for x in sigs} == {"momentum_breakout", "vwap_reclaim", "pullback"}
    assert all(x.feed == "iex" and x.config_version == 3 for x in sigs)
    (dec,) = decs
    assert dec.decision_result == "BLOCKED"
    assert dec.block_reasons == ["NO_BROKER", "PAUSED_BY_USER"]
    assert dec.ai_mode == "OFF" and dec.llm_verdict is None
    assert "Model confidence" in dec.reasoning
    linked = next(x for x in sigs if x.id == dec.signal_id)
    assert linked.action == "BUY" and linked.conditions["breakout"]["passed"] is True
    assert linked.features["price"] == 101.5

    # Same minute again: nothing new (each bar is evaluated once).
    assert await svc.cycle(CAL, ["AAA"], CFG, 3, ["NO_BROKER"]) is None
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(TradingSignal))).scalar() == 3


async def test_no_cycle_outside_the_session(sessionmaker: Any) -> None:
    svc = DecisionService(sessionmaker, feed="iex",
                          clock=lambda: SESSION.open_at - timedelta(minutes=10))
    assert await svc.cycle(CAL, ["AAA"], CFG, 1, []) is None


async def test_decision_and_signal_api(auth_client: httpx.AsyncClient, sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    await DecisionService(sessionmaker, feed="iex", clock=lambda: AT).cycle(
        CAL, ["AAA"], CFG, 3, ["NO_BROKER"])

    decs = (await auth_client.get(f"{API}/decisions")).json()
    assert len(decs) == 1 and decs[0]["decision_result"] == "BLOCKED"
    assert Decimal(decs[0]["entry_ref_price"]) == Decimal("101.50")
    assert Decimal(decs[0]["stop_loss"]) == Decimal("100.49")
    detail = (await auth_client.get(f"{API}/decisions/{decs[0]['id']}")).json()
    assert detail["signal"]["conditions"]["breakout"]["passed"] is True
    assert detail["signal"]["factor_scores"]["news"] == 0.5
    assert (await auth_client.get(f"{API}/decisions/{'0' * 32}")).status_code == 404

    sigs = (await auth_client.get(f"{API}/signals", params={"symbol": "aaa"})).json()
    assert len(sigs) == 3
    buys = (await auth_client.get(f"{API}/signals", params={"action": "BUY"})).json()
    assert [x["strategy"] for x in buys] == ["momentum_breakout"]
    assert (await auth_client.get(f"{API}/regime")).json()["regime"] is None  # no engine yet


async def test_engine_decision_tick_respects_data_staleness(env: EnvSettings,
                                                            sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    rt = EngineRuntime(env, sessionmaker, None, TradingConfig(), clock=lambda: AT)
    await rt.boot()
    rt.calendar = CAL
    rt.decisions = DecisionService(sessionmaker, feed="iex", clock=lambda: AT)
    rt.scanner = SimpleNamespace(passed=frozenset({"AAA"}), last=None)  # type: ignore[assignment]

    rt.gate.set("DATA_STALE", AT)  # stale data → no cycle
    await rt.decision_tick()
    assert rt.decisions.last is None

    rt.gate.clear("DATA_STALE")
    await rt.decision_tick()
    assert rt.decisions.last is not None and rt.decisions.last.proposals
    await rt.write_state()
    async with sessionmaker() as s:
        dec = (await s.execute(select(AiDecision))).scalar_one()
    assert "NO_BROKER" in dec.block_reasons


def test_offline_replay_cli(db_url: str, sessionmaker: Any, monkeypatch: pytest.MonkeyPatch
                            ) -> None:
    import asyncio

    asyncio.run(_seed_in_new_loop(db_url))
    monkeypatch.setenv("DATABASE_URL", db_url)
    env_module.get_env.cache_clear()
    from intraday.cli import app

    res = CliRunner().invoke(app, ["signals", "--date", "2026-09-21", "--symbol", "AAA"])
    env_module.get_env.cache_clear()
    assert res.exit_code == 0, res.output
    assert f"cycles={N}" in res.output and "proposals=" in res.output
    assert "AAA    momentum_breakout  BUY" in res.output
    missing = CliRunner().invoke(app, ["signals", "--date", "2026-09-19"])
    assert missing.exit_code == 1


async def _seed_in_new_loop(db_url: str) -> None:
    from intraday.db.session import make_engine, make_sessionmaker

    engine = make_engine(db_url, pool_size=1)
    await seed_history(make_sessionmaker(engine))
    await engine.dispose()


_ = datetime  # keep import for type clarity in fixtures
