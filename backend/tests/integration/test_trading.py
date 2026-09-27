"""Phase 8: order manager, positions, exits, reconciliation, liquidation (SimBroker + DB)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from intraday.core.ids import uuid7
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import (
    AiDecision,
    Alert,
    DailyPnl,
    Fill,
    Order,
    Position,
    Symbol,
    TradingSignal,
)
from intraday.db.repositories import trading as repo
from intraday.db.session import transaction
from intraday.marketdata.calendar import entry_window
from intraday.marketdata.types import Bar
from intraday.trading.broker.base import BrokerError, BrokerOrder
from intraday.trading.broker.sim_broker import SimBroker
from intraday.trading.liquidation import Liquidator
from intraday.trading.order_manager import EntryRequest, OrderManager
from intraday.trading.pricing import client_order_id, price_entry
from intraday.trading.reconciler import Reconciler
from tests.session_fixtures import SESSION

D = Decimal
T0 = SESSION.open_at + timedelta(minutes=10)


@dataclass
class Clock:
    """Advances 1 ms per reading, so rows created in sequence keep their order."""

    now: datetime = T0

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=1)
        return self.now


async def no_sleep(_: float) -> None:
    return None


def bar(minute: int, o: float, h: float, lo: float, c: float, symbol: str = "AAA") -> Bar:
    d = lambda x: D(str(x))  # noqa: E731
    return Bar(symbol, "iex", SESSION.open_at + timedelta(minutes=minute), d(o), d(h), d(lo),
               d(c), 1000, d(c))


async def add_symbol(sm: Any, symbol: str = "AAA") -> None:
    async with transaction(sm) as s:
        s.add(Symbol(symbol=symbol, role="TRADABLE", asset_type="COMMON_STOCK", country="US",
                     sector="Technology", alpaca_class="us_equity", alpaca_exchange="NASDAQ",
                     alpaca_name=f"{symbol} Inc. Common Stock", alpaca_tradable=True,
                     alpaca_status="active", alpaca_synced_at=T0))


def make(sm: Any, broker: Any = None, **kw: Any) -> tuple[Any, OrderManager, Clock]:
    clock = Clock()
    sim = broker or SimBroker()
    om = OrderManager(sm, sim, clock=clock, sleep=no_sleep, retry_delays=(0.0, 0.0, 0.0),
                      **kw)
    return sim, om, clock


async def entry(sm: Any, symbol: str = "AAA", qty: int = 10, limit: str = "100.10",
                stop: str = "99.00", target: str = "102.00") -> EntryRequest:
    """An entry for a real (persisted) decision, as the decision loop produces."""
    sid, did = uuid7(), uuid7()
    async with transaction(sm) as s:
        s.add(TradingSignal(id=sid, bar_ts=T0, session_date=SESSION.session_date, symbol=symbol,
                            strategy="momentum_breakout", feed="iex", features={},
                            conditions={}, factor_scores={}, score=D("0.8"), regime="BULLISH",
                            signals=[], action="BUY", config_version=1))
        await s.flush()
        s.add(AiDecision(id=did, signal_id=sid, symbol=symbol, strategy="momentum_breakout",
                         market_price=D(limit), action="BUY", confidence=D("0.8"),
                         score=D("0.8"), reasoning="", entry_ref_price=D(limit),
                         stop_loss=D(stop), take_profit=D(target), risk_reward=D("2"),
                         expected_return=D("0.01"), estimated_risk=D("0.01"), ai_mode="OFF",
                         decision_result="ORDER_SUBMITTED", model_version="rules-v1",
                         config_version=1))
    return EntryRequest(did, SESSION.session_date, symbol, qty, D(limit), D(stop),
                        D(target), strategy="momentum_breakout", score=D("0.8"))


async def position(sm: Any, pid: Any) -> Position:
    async with sm() as s:
        return await repo.get_position(s, pid)


async def orders(sm: Any, pid: Any = None) -> list[Order]:
    async with sm() as s:
        q = select(Order).order_by(Order.created_at)
        if pid is not None:
            q = q.where(Order.position_id == pid)
        return list((await s.execute(q)).scalars())


async def open_position(sm: Any, sim: SimBroker, om: OrderManager, **kw: Any) -> Any:
    await add_symbol(sm, kw.get("symbol", "AAA"))
    res = await om.submit_entry(await entry(sm, **kw))
    assert res.status == "SUBMITTED"
    sim.on_bar(bar(11, 100.0, 100.3, 99.9, 100.2, kw.get("symbol", "AAA")))
    await sim.drain(om.on_trade_update)
    return res.position_id


# ------------------------------------------------------------------ pure helpers

def test_price_entry_and_client_order_id() -> None:
    px = price_entry(D("100.00"), D("99.00"), D("102.00"), D("100.02"), 5)
    assert px.limit == D("100.05") and px.stop == D("99.05") and px.target == D("102.05")
    # Ask above the signal close: never chase above the reference plus the offset.
    assert price_entry(D("100"), D("99"), D("102"), D("101"), 5).limit == D("100.05")
    assert price_entry(D("100"), D("99"), D("102"), None, 0).limit == D("100.00")
    cid = client_order_id(SESSION.session_date, "ENTRY",
                          "0192f3a1-7b2c-7def-8123-4567f3a9c21e", 0)
    assert cid == "ia-20260921-EN-f3a9c21e-0" and len(cid) <= 128


# ------------------------------------------------------------------ lifecycle

async def test_full_lifecycle_entry_legs_take_profit(sessionmaker: Any) -> None:
    sim, om, _ = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    pos = await position(sessionmaker, pid)
    assert pos.status == "OPEN" and pos.qty == 10 and pos.avg_entry_price == D("100.05")
    rows = await orders(sessionmaker, pid)
    assert [(o.intent, o.status) for o in rows] == [
        ("ENTRY", "FILLED"), ("TAKE_PROFIT", "SUBMITTED"), ("STOP_LOSS", "SUBMITTED")]
    assert rows[0].client_order_id.startswith("ia-20260921-EN-")

    sim.on_bar(bar(12, 100.2, 102.5, 100.1, 102.2))
    await sim.drain(om.on_trade_update)
    pos = await position(sessionmaker, pid)
    assert pos.status == "CLOSED" and pos.exit_reason == "TAKE_PROFIT" and pos.qty == 0
    assert pos.net_pnl == D("19.50") and pos.avg_exit_price == D("102.00")
    assert [o.status for o in await orders(sessionmaker, pid)] == ["FILLED", "FILLED", "CANCELED"]
    async with sessionmaker() as s:
        day = await s.get(DailyPnl, SESSION.session_date)
        fills = (await s.execute(select(Fill))).scalars().all()
    assert day.trades == 1 and day.wins == 1 and day.realized_net == D("19.50")
    assert len(fills) == 2


async def test_stream_poll_and_reconcile_never_double_count_fills(sessionmaker: Any) -> None:
    sim, om, _ = make(sessionmaker)
    await add_symbol(sessionmaker)
    res = await om.submit_entry(await entry(sessionmaker))
    sim.on_bar(bar(11, 100.0, 100.3, 99.9, 100.2))
    updates: list[Any] = []

    async def keep(u: Any) -> None:
        updates.append(u)

    await sim.drain(keep)
    await om.poll_open_orders()              # the poll sees the fill first
    for u in updates:                        # then the (late) stream delivers it again
        await om.on_trade_update(u)
    await om.poll_open_orders("RECONCILE")
    pos = await position(sessionmaker, res.position_id)
    assert pos.qty == 10
    async with sessionmaker() as s:
        assert len((await s.execute(select(Fill))).scalars().all()) == 1


# ------------------------------------------------------------------ idempotency

async def test_timeout_after_broker_accepted_is_adopted_not_resubmitted(sessionmaker: Any
                                                                         ) -> None:
    sim, om, _ = make(sessionmaker)
    await add_symbol(sessionmaker)
    sim.fail_submit = "after"                # accepted at the broker, then the response is lost
    res = await om.submit_entry(await entry(sessionmaker))
    assert res.status == "SUBMITTED"
    assert sum(1 for o in sim.orders.values() if o.parent is None) == 1
    (row, *_) = await orders(sessionmaker)
    assert row.status == "SUBMITTED" and row.broker_order_id and row.attempts == 1


async def test_network_error_before_accept_retries_with_same_client_id(sessionmaker: Any
                                                                       ) -> None:
    sim, om, _ = make(sessionmaker)
    await add_symbol(sessionmaker)
    sim.fail_submit = "before"
    res = await om.submit_entry(await entry(sessionmaker))
    assert res.status == "SUBMITTED"
    parents = [o for o in sim.orders.values() if o.parent is None]
    assert len(parents) == 1
    (row, *_) = await orders(sessionmaker)
    assert parents[0].cid == row.client_order_id


class DownBroker(SimBroker):
    """Every submission times out and never reaches the broker."""

    async def submit_bracket_entry(self, order: Any) -> BrokerOrder:
        raise BrokerError(0, "network error: ReadTimeout", retryable=True)


async def test_unknown_after_retries_blocks_then_restart_resolves(sessionmaker: Any) -> None:
    sim, om, _ = make(sessionmaker, DownBroker())
    await add_symbol(sessionmaker)
    res = await om.submit_entry(await entry(sessionmaker))
    assert res.status == "UNKNOWN" and om.broker_down_reason
    (row, *_) = await orders(sessionmaker)
    assert row.status == "UNKNOWN" and row.attempts == 4
    # Restart: a fresh order manager settles it by client_order_id before trading.
    _, om2, _ = make(sessionmaker, sim)
    assert await om2.resolve_pending(min_age_seconds=0) == 0
    (row, *_) = await orders(sessionmaker)
    assert row.status == "FAILED"
    assert (await position(sessionmaker, res.position_id)).status == "ABANDONED"


async def test_restart_mid_submit_adopts_order_found_at_broker(sessionmaker: Any) -> None:
    sim, om, _ = make(sessionmaker)
    await add_symbol(sessionmaker)
    req = await entry(sessionmaker)
    cid = client_order_id(req.session_date, "ENTRY", req.decision_id, 0)
    pid = uuid7()
    async with transaction(sessionmaker) as s:   # crash after persisting, before the response
        s.add(Position(id=pid, session_date=req.session_date, symbol="AAA",
                       status="PENDING_ENTRY", qty=0))
        await s.flush()
        s.add(Order(client_order_id=cid, position_id=pid, symbol="AAA", side="buy",
                    intent="ENTRY", type="limit", order_class="bracket", tif="day", qty=10,
                    limit_price=D("100.10"), status="PENDING_SUBMIT", created_at=T0))
    from intraday.trading.broker.base import BracketEntry
    await sim.submit_bracket_entry(BracketEntry(cid, "AAA", 10, D("100.10"), D("102"), D("99")))
    assert await om.resolve_pending(min_age_seconds=0) == 0
    rows = await orders(sessionmaker, pid)
    assert rows[0].status == "SUBMITTED" and len(rows) == 3   # parent adopted, legs linked
    assert sum(1 for o in sim.orders.values() if o.parent is None) == 1


class RejectingBroker(SimBroker):
    async def submit_bracket_entry(self, order: Any) -> BrokerOrder:
        raise BrokerError(403, "insufficient buying power")


async def test_rejected_entry_abandons_position(sessionmaker: Any) -> None:
    _, om, _ = make(sessionmaker, RejectingBroker())
    await add_symbol(sessionmaker)
    res = await om.submit_entry(await entry(sessionmaker))
    assert res.status == "REJECTED" and "buying power" in (res.detail or "")
    assert (await position(sessionmaker, res.position_id)).status == "ABANDONED"
    (row, *_) = await orders(sessionmaker)
    assert row.status == "REJECTED" and row.attempts == 1   # 4xx: never retried


async def test_entry_invariants(sessionmaker: Any) -> None:
    sim, om, _ = make(sessionmaker, entry_blocks=lambda: ["PAUSED_BY_USER"])
    await add_symbol(sessionmaker)
    assert (await om.submit_entry(await entry(sessionmaker))).status == "BLOCKED"
    om.entry_blocks = None
    assert (await om.submit_entry(await entry(sessionmaker, stop="100.20"))).status == "FAILED"
    assert (await om.submit_entry(await entry(sessionmaker, limit="100.105"))).status == "FAILED"
    assert (await om.submit_entry(await entry(sessionmaker, qty=0))).status == "FAILED"
    bad = await om.submit_entry(await entry(sessionmaker, symbol="ZZZ"))
    assert bad.status == "FAILED" and "eligible" in (bad.detail or "")
    assert (await om.submit_entry(await entry(sessionmaker))).status == "SUBMITTED"
    dup = await om.submit_entry(await entry(sessionmaker))
    assert dup.status == "FAILED" and "live position" in (dup.detail or "")
    assert not [o for o in sim.orders.values() if o.symbol == "ZZZ"]


# ------------------------------------------------------------------ timeouts

async def test_unfilled_entry_is_cancelled_after_timeout(sessionmaker: Any) -> None:
    sim, om, clock = make(sessionmaker)
    await add_symbol(sessionmaker)
    res = await om.submit_entry(await entry(sessionmaker))
    sim.on_bar(bar(11, 101.0, 101.5, 100.5, 101.2))      # never trades down to the limit
    await sim.drain(om.on_trade_update)
    assert await om.check_entry_timeouts(30) == 0
    clock.now += timedelta(seconds=31)
    assert await om.check_entry_timeouts(30) == 1
    await sim.drain(om.on_trade_update)
    assert (await position(sessionmaker, res.position_id)).status == "ABANDONED"
    assert {o.status for o in await orders(sessionmaker, res.position_id)} == {"CANCELED"}


# ------------------------------------------------------------------ exits

async def test_engine_exit_cancels_legs_then_sells(sessionmaker: Any) -> None:
    sim, om, _ = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    assert await om.exit_position(pid, "TIME_EXIT") == "SUBMITTED"
    assert await om.exit_position(pid, "TIME_EXIT") == "ALREADY_EXITING"
    await sim.drain(om.on_trade_update)
    rows = await orders(sessionmaker, pid)
    assert [(o.intent, o.status) for o in rows] == [
        ("ENTRY", "FILLED"), ("TAKE_PROFIT", "CANCELED"), ("STOP_LOSS", "CANCELED"),
        ("ENGINE_EXIT", "SUBMITTED")]
    assert rows[-1].client_order_id.startswith("ia-20260921-EX-")
    assert (await position(sessionmaker, pid)).status == "EXITING"
    sim.on_bar(bar(12, 100.5, 100.6, 100.4, 100.5))
    await sim.drain(om.on_trade_update)
    pos = await position(sessionmaker, pid)
    assert pos.status == "CLOSED" and pos.exit_reason == "TIME_EXIT"


async def test_sell_qty_never_exceeds_broker_qty(sessionmaker: Any) -> None:
    sim, om, _ = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    async with transaction(sessionmaker) as s:   # local state drifted above the broker's
        p = await repo.get_position(s, pid, lock=True)
        p.qty = 25
    await om.exit_position(pid, "MOMENTUM_REVERSAL")
    exit_row = (await orders(sessionmaker, pid))[-1]
    assert exit_row.intent == "ENGINE_EXIT" and exit_row.qty == 10


async def test_leg_filling_during_exit_closes_by_that_leg(sessionmaker: Any) -> None:
    sim, om, _ = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    sim.on_bar(bar(12, 100.2, 100.3, 98.5, 98.8))           # stop fills at the broker...
    # ...but the stream update is not delivered before the engine decides to exit.
    assert await om.exit_position(pid, "VWAP_LOSS") == "CLOSED_BY_LEG"
    pos = await position(sessionmaker, pid)
    assert pos.status == "CLOSED" and pos.exit_reason == "STOP_LOSS"
    assert not [o for o in sim.orders.values() if o.type == "market"]


async def test_trailing_stop_only_raised(sessionmaker: Any) -> None:
    sim, om, _ = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    assert await om.raise_stop(pid, D("98.50")) is False          # lower: refused
    assert await om.raise_stop(pid, D("100.40")) is True
    await sim.drain(om.on_trade_update)
    pos = await position(sessionmaker, pid)
    assert pos.stop_price == D("100.40")
    stops = [o for o in await orders(sessionmaker, pid) if o.intent == "STOP_LOSS"]
    assert [(o.status, o.stop_price) for o in stops] == [("REPLACED", D("99.0000")),
                                                        ("SUBMITTED", D("100.40"))]
    assert await om.raise_stop(pid, D("100.40")) is False
    async with transaction(sessionmaker) as s:
        p = await repo.get_position(s, pid, lock=True)
        p.trailing_active = True
    sim.on_bar(bar(12, 100.5, 100.6, 100.2, 100.3))
    await sim.drain(om.on_trade_update)
    pos = await position(sessionmaker, pid)
    assert pos.status == "CLOSED" and pos.exit_reason == "TRAILING_STOP"


async def test_stuck_exit_alerts_and_reprotects(sessionmaker: Any) -> None:
    sim, om, clock = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    await om.exit_position(pid, "TIME_EXIT")
    await sim.drain(om.on_trade_update)
    clock.now += timedelta(seconds=25)                    # no bar: the market sell never fills
    assert await om.check_exit_timeouts(20) == 1
    await sim.drain(om.on_trade_update)
    pos = await position(sessionmaker, pid)
    assert pos.status == "OPEN" and pos.exit_detail["exit_retries"] == 1
    live = [o for o in await orders(sessionmaker, pid) if o.status == "SUBMITTED"]
    assert {(o.intent, o.order_class) for o in live} == {("TAKE_PROFIT", "oco"),
                                                        ("STOP_LOSS", "oco")}
    async with sessionmaker() as s:
        types = {a.type for a in (await s.execute(select(Alert))).scalars()}
    assert {"EXIT_NOT_FILLED", "PROTECTION_REPLACED"} <= types


# ------------------------------------------------------------------ reconciliation

async def test_reconcile_clean(sessionmaker: Any) -> None:
    sim, om, clock = make(sessionmaker)
    await open_position(sessionmaker, sim, om)
    clock.now += timedelta(seconds=10)
    report = await Reconciler(sessionmaker, sim, om, clock=clock).run()
    assert report.clean and not report.actions


async def test_reconcile_applies_missed_stop_fill_without_mismatch(sessionmaker: Any) -> None:
    sim, om, clock = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    sim.on_bar(bar(12, 100.2, 100.3, 98.5, 98.8))
    sim.queue = type(sim.queue)()                          # stream updates lost
    clock.now += timedelta(seconds=10)
    report = await Reconciler(sessionmaker, sim, om, clock=clock).run()
    assert report.clean
    pos = await position(sessionmaker, pid)
    assert pos.status == "CLOSED" and pos.exit_reason == "STOP_LOSS"


async def test_reconcile_mismatches(sessionmaker: Any) -> None:
    sim, om, clock = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    clock.now += timedelta(seconds=10)
    rec = Reconciler(sessionmaker, sim, om, clock=clock)

    sim.positions["BBB"] = (5, D("50"))                    # unknown to the engine
    report = await rec.run()
    assert [m["type"] for m in report.mismatches] == ["UNEXPECTED_POSITION"]

    sim.positions.pop("BBB")
    sim.positions["AAA"] = (7, D("100.05"))                # broker holds fewer shares
    report = await rec.run()
    assert report.mismatches == [{"symbol": "AAA", "type": "QTY_MISMATCH", "local_qty": 10,
                                  "broker_qty": 7}]
    assert (await position(sessionmaker, pid)).qty == 7   # broker is truth

    sim.positions.pop("AAA")                               # flat with nothing explaining it
    clock.now += timedelta(seconds=10)                     # past the in-flight window
    report = await rec.run()
    assert [m["type"] for m in report.mismatches] == ["BROKER_FLAT"]


async def test_reconcile_replaces_missing_protection(sessionmaker: Any) -> None:
    sim, om, clock = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    for o in list(sim.orders.values()):                    # legs cancelled outside the engine
        if o.parent is not None:
            await sim.cancel_order(o.id)
    sim.queue = type(sim.queue)()
    clock.now += timedelta(seconds=10)
    report = await Reconciler(sessionmaker, sim, om, clock=clock).run()
    assert any("SUBMITTED" in a for a in report.actions)
    ocos = [o for o in sim.orders.values() if o.order_class == "oco" and o.status == "new"]
    assert len(ocos) == 2 and all(o.qty == 10 for o in ocos)
    assert (await position(sessionmaker, pid)).status == "OPEN"


# ------------------------------------------------------------------ liquidation

async def test_liquidate_sells_every_broker_position(sessionmaker: Any) -> None:
    sim, om, clock = make(sessionmaker)
    p1 = await open_position(sessionmaker, sim, om)
    sim.positions["BBB"] = (5, D("50"))                    # unknown locally: still closed
    liq = Liquidator(sessionmaker, sim, om, clock=clock, sleep=no_sleep)
    result = await liq.liquidate("EMERGENCY", "EMERGENCY_CLOSE")
    assert sorted((x["symbol"], x["qty"]) for x in result.submitted) == [("AAA", 10),
                                                                          ("BBB", 5)]
    await sim.drain(om.on_trade_update)
    sim.on_bar(bar(12, 100.5, 100.6, 100.4, 100.5))
    sim.on_bar(bar(12, 50, 50.1, 49.9, 50, "BBB"))
    await sim.drain(om.on_trade_update)
    assert await liq.is_flat()
    pos = await position(sessionmaker, p1)
    assert pos.status == "CLOSED" and pos.exit_reason == "EMERGENCY"


async def test_eod_force_close_escalates_when_not_flat(sessionmaker: Any) -> None:
    sim, om, clock = make(sessionmaker)
    await open_position(sessionmaker, sim, om)
    liq = Liquidator(sessionmaker, sim, om, clock=clock, sleep=no_sleep)
    window = entry_window(SESSION, TradingConfig().hours)
    await liq.eod_tick(window.force_close - timedelta(seconds=5), window)
    assert liq.eod is not None and not liq.eod.started
    await liq.eod_tick(window.force_close, window)
    assert liq.eod.started
    # No bars arrive, so the market sell never fills.
    await liq.eod_tick(window.force_close + timedelta(minutes=3, seconds=1), window)
    await liq.eod_tick(SESSION.close_at - timedelta(seconds=20), window)
    async with sessionmaker() as s:
        alerts = {(a.severity, a.type) for a in (await s.execute(select(Alert))).scalars()}
    assert ("HIGH", "NOT_FLAT") in alerts and ("CRITICAL", "NOT_FLAT") in alerts
    assert await liq.overnight_check() == ["AAA x10"]


async def test_eod_force_close_success(sessionmaker: Any) -> None:
    sim, om, clock = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    liq = Liquidator(sessionmaker, sim, om, clock=clock, sleep=no_sleep)
    window = entry_window(SESSION, TradingConfig().hours)
    await liq.eod_tick(window.force_close, window)
    await sim.drain(om.on_trade_update)
    sim.on_bar(bar(386, 100.5, 100.6, 100.4, 100.5))
    await sim.drain(om.on_trade_update)
    await liq.eod_tick(window.force_close + timedelta(seconds=10), window)
    assert liq.eod.done
    pos = await position(sessionmaker, pid)
    assert pos.status == "CLOSED" and pos.exit_reason == "EOD_LIQUIDATION"
    async with sessionmaker() as s:
        assert not (await s.execute(select(Alert))).scalars().all()


@pytest.mark.parametrize("fail", ["before", "after"])
async def test_exit_submission_is_idempotent(sessionmaker: Any, fail: str) -> None:
    sim, om, _ = make(sessionmaker)
    pid = await open_position(sessionmaker, sim, om)
    sim.fail_submit = fail
    assert await om.exit_position(pid, "TIME_EXIT") == "SUBMITTED"
    assert len([o for o in sim.orders.values() if o.type == "market"]) == 1
