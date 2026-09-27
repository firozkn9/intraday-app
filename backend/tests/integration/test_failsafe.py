"""Phase 15 failure injection: SPEC §27 fail-safe behaviour on the engine runtime.

Each failure must stop new trades; a restart must reconcile with the broker
before any new trade and never assume there are no positions.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from intraday.core.env import EnvSettings
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import Order, Position
from intraday.engine.runtime import EngineRuntime
from intraday.engine.spool import TradeUpdateSpool
from intraday.trading.broker.sim_broker import SimBroker
from tests.integration.test_trading import add_symbol, bar, entry, no_sleep


def runtime(env: EnvSettings, sm: Any, sim: SimBroker, cfg: TradingConfig | None = None
            ) -> EngineRuntime:
    return EngineRuntime(env, sm, None, cfg or TradingConfig(), broker_factory=lambda: sim,
                         order_manager_options={"sleep": no_sleep, "retry_delays": (0, 0, 0)},
                         sleep=no_sleep)


async def open_position(rt: EngineRuntime, sim: SimBroker, sm: Any) -> Any:
    await add_symbol(sm)
    assert rt.om is not None
    rt.om.entry_blocks = None
    res = await rt.om.submit_entry(await entry(sm))
    sim.on_bar(bar(11, 100.0, 100.3, 99.9, 100.2))
    await sim.drain(rt.om.on_trade_update)
    return res.position_id


async def broker_order_count(sim: SimBroker) -> int:
    return sum(1 for o in sim.orders.values() if o.parent is None)


async def test_restart_mid_session_reconciles_and_keeps_the_position(
        env: EnvSettings, sessionmaker: Any) -> None:
    sim = SimBroker()
    rt1 = runtime(env, sessionmaker, sim)
    await rt1.boot()
    pid = await open_position(rt1, sim, sessionmaker)
    before = await broker_order_count(sim)
    # The engine process dies (no shutdown). A new one starts against the same broker.
    rt2 = runtime(env, sessionmaker, sim)
    rt2.gate.set("RECONCILING", rt2.clock())
    await rt2.boot()
    assert "RECONCILING" not in rt2.gate.blocks and "RECONCILE_MISMATCH" not in rt2.gate.blocks
    assert await broker_order_count(sim) == before            # no duplicate orders
    async with sessionmaker() as s:
        pos = await s.get(Position, pid)
        stops = [o for o in (await s.execute(select(Order).where(Order.position_id == pid)))
                 .scalars() if o.intent == "STOP_LOSS" and o.status == "SUBMITTED"]
    assert pos.status == "OPEN" and pos.qty == 10 and len(stops) == 1


async def test_restart_after_stop_filled_while_down(env: EnvSettings, sessionmaker: Any) -> None:
    sim = SimBroker()
    rt1 = runtime(env, sessionmaker, sim)
    await rt1.boot()
    pid = await open_position(rt1, sim, sessionmaker)
    sim.on_bar(bar(12, 100.2, 100.3, 98.5, 98.8))            # stop fills; nobody listening
    sim.queue = type(sim.queue)()
    rt2 = runtime(env, sessionmaker, sim)
    await rt2.boot()
    assert "RECONCILE_MISMATCH" not in rt2.gate.blocks
    async with sessionmaker() as s:
        pos = await s.get(Position, pid)
    assert pos.status == "CLOSED" and pos.exit_reason == "STOP_LOSS"


async def test_restart_never_assumes_flat(env: EnvSettings, sessionmaker: Any) -> None:
    sim = SimBroker()
    sim.positions["AAA"] = (10, Decimal("100"))               # held at the broker only
    rt = runtime(env, sessionmaker, sim)
    await rt.boot()
    assert "RECONCILE_MISMATCH" in rt.gate.latched and not rt.gate.entries_allowed


async def test_broker_api_failure_stops_entries_until_recovered(
        env: EnvSettings, sessionmaker: Any) -> None:
    sim = SimBroker()
    rt = runtime(env, sessionmaker, sim)
    await rt.boot()
    sim.fail_reads = True
    rt._last_reconcile = None
    await rt.trading_tick()
    assert "BROKER_DOWN" in rt.gate.blocks and not rt.gate.entries_allowed
    sim.fail_reads = False
    rt._last_reconcile = rt._last_poll = None
    await rt.trading_tick()
    assert "BROKER_DOWN" not in rt.gate.blocks


async def test_stream_drop_triggers_reconcile_that_applies_missed_fills(
        env: EnvSettings, sessionmaker: Any) -> None:
    sim = SimBroker()
    rt = runtime(env, sessionmaker, sim)
    await rt.boot()
    pid = await open_position(rt, sim, sessionmaker)
    await rt._on_stream_state("connected")
    await rt._on_stream_state("disconnected")
    sim.on_bar(bar(12, 100.2, 102.5, 100.1, 102.2))           # target fills during the gap
    sim.queue = type(sim.queue)()
    await rt._on_stream_state("connected")
    assert rt._wake_reconcile.is_set()
    await rt.trading_tick()
    async with sessionmaker() as s:
        pos = await s.get(Position, pid)
    assert pos.status == "CLOSED" and pos.exit_reason == "TAKE_PROFIT"


async def test_database_outage_spools_trade_updates(env: EnvSettings, sessionmaker: Any) -> None:
    sim = SimBroker()
    rt = runtime(env, sessionmaker, sim)
    await rt.boot()
    assert rt.om is not None
    pid = await open_position(rt, sim, sessionmaker)
    down = True
    real = rt.om.on_trade_update

    async def flaky(u: Any) -> None:
        if down:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        await real(u)

    spool = TradeUpdateSpool(flaky)
    sim.on_bar(bar(12, 100.2, 102.5, 100.1, 102.2))
    updates: list[Any] = []

    async def keep(u: Any) -> None:
        updates.append(u)

    await sim.drain(keep)
    for u in updates:
        await spool.handle(u)
    assert len(spool) == len(updates) > 0
    rt.gate.set("DB_DOWN", rt.clock())
    assert not rt.gate.entries_allowed                        # DB down: no new trades
    down = False
    assert await spool.drain() == len(updates)
    async with sessionmaker() as s:
        assert (await s.get(Position, pid)).status == "CLOSED"


async def test_ai_unavailable_in_veto_mode_stops_entries(env: EnvSettings, sessionmaker: Any
                                                          ) -> None:
    cfg = TradingConfig.model_validate({"ai": {"mode": "VETO", "model": "some-model"}})
    rt = EngineRuntime(env, sessionmaker, None, cfg, ai_provider_factory=lambda c: None)
    await rt.boot()
    rt.config = cfg
    rt._ensure_ai()
    assert "AI_UNAVAILABLE" in rt.gate.blocks and not rt.gate.entries_allowed
