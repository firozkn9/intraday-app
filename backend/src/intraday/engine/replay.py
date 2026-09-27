"""Replay mode (SPEC §39; docs/design/08 §10, roadmap Phase 6/8/15).

Drives the **live** trading stack over one stored session, minute by minute,
with a replay clock and the SimBroker: DecisionService (signals → risk →
bracket entries via the OrderManager), trade updates, ExitEngine, entry
timeouts and the end-of-day Liquidator. Everything is written to the database
exactly as in paper trading, which is why it refuses to run against a database
whose name doesn't mark it as a simulation database (``*sim*``, ``*replay*``,
``*test*`` or ``*e2e*``) unless explicitly allowed.

Differences from live, by design: quotes are synthesised from the bar close
(1-cent half spread), the universe is the enabled tradable watchlist (no scan),
and the AI layer is off.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.logging import get_logger
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import DailyPnl, Position
from intraday.db.repositories import trading as trading_repo
from intraday.engine.decisions import DecisionService, load_session_bars
from intraday.engine.risk_adapter import RiskAdapter
from intraday.features.regime import REFERENCE
from intraday.marketdata.calendar import MarketCalendar, entry_window
from intraday.marketdata.types import BAR_INTERVAL, Bar, Quote
from intraday.trading.broker.sim_broker import SimBroker
from intraday.trading.exit_engine import ExitEngine
from intraday.trading.liquidation import Liquidator
from intraday.trading.order_manager import EntryRequest, OrderManager

log = get_logger(__name__)

SIM_DB = re.compile(r"(sim|replay|test|e2e)", re.IGNORECASE)
CENT = Decimal("0.01")


class ReplayDatabaseError(RuntimeError):
    pass


def check_database(url: str, *, allow_any: bool = False) -> str:
    name = make_url(url).database or ""
    if not allow_any and not SIM_DB.search(name):
        raise ReplayDatabaseError(
            f"refusing to replay into database {name!r}: replay writes orders and positions. "
            "Use a simulation database (name containing sim/replay/test/e2e).")
    return name


@dataclass
class ReplayClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class ReplayReport:
    session_date: date
    minutes: int = 0
    proposals: int = 0
    submitted: int = 0
    trades: list[dict[str, Any]] = field(default_factory=list)
    realized_net: Decimal = Decimal(0)
    flat_at_close: bool = True

    def to_json(self) -> dict[str, Any]:
        return {"session_date": self.session_date.isoformat(), "minutes": self.minutes,
                "proposals": self.proposals, "submitted": self.submitted,
                "trades": self.trades, "realized_net": str(self.realized_net),
                "flat_at_close": self.flat_at_close}


async def _no_sleep(_: float) -> None:
    return None


async def replay_session(sm: async_sessionmaker[AsyncSession], calendar: MarketCalendar,
                         day: date, cfg: TradingConfig, config_version: int,
                         universe: Sequence[str], *, feed: str = "iex",
                         on_event: Callable[[str], None] | None = None) -> ReplayReport:
    session = calendar.session(day)
    if session is None:
        raise ValueError(f"{day} is not a trading session in the stored calendar")
    say = on_event or (lambda _: None)
    symbols = sorted(set(universe) | set(REFERENCE))
    bars = await load_session_bars(sm, feed, session, symbols, session.close_at)
    by_minute: dict[datetime, list[Bar]] = defaultdict(list)
    for sym_bars in bars.values():
        for b in sym_bars:
            by_minute[b.ts].append(b)
    last_bar: dict[str, Bar] = {}

    clock = ReplayClock(session.open_at)
    sim = SimBroker(cfg.risk.paper_initial_balance)
    om = OrderManager(sm, sim, clock=clock, sleep=_no_sleep, retry_delays=(0, 0, 0),
                      cancel_confirm_seconds=0)

    async def quotes(syms: Sequence[str]) -> dict[str, Quote]:
        return {s: Quote(s, feed, clock() - timedelta(seconds=1), last_bar[s].close - CENT, 100,
                         last_bar[s].close + CENT, 100) for s in syms if s in last_bar}

    risk = RiskAdapter(sm, feed=feed, quotes=quotes, clock=clock)
    decisions = DecisionService(sm, feed=feed, clock=clock)
    exits = ExitEngine(sm, om, clock=clock)
    liquidator = Liquidator(sm, sim, om, clock=clock, sleep=_no_sleep)
    window = entry_window(session, cfg.hours)
    report = ReplayReport(day)

    async def submit(did: Any, rd: Any, rec: Any) -> tuple[str, list[str]]:
        p = rd.proposal
        res = await om.submit_entry(EntryRequest(
            did, day, p.symbol, rd.qty, p.entry, p.stop, p.target, strategy=rec.strategy,
            score=Decimal(str(round(rec.score, 4))), config_version=config_version))
        say(f"{clock():%H:%M} ENTRY {p.symbol} x{rd.qty} @ {p.entry} "
            f"(stop {p.stop}, target {p.target}): {res.status}")
        if res.status == "SUBMITTED":
            report.submitted += 1
            return "ORDER_SUBMITTED", []
        return "ORDER_FAILED", [res.status]

    for minute in sorted(by_minute):
        report.minutes += 1
        clock.now = minute + BAR_INTERVAL
        for b in by_minute[minute]:
            last_bar[b.symbol] = b
            sim.on_bar(b)                      # orders sent earlier fill on this bar
        await sim.drain(om.on_trade_update)
        clock.now = minute + BAR_INTERVAL + timedelta(seconds=6)
        blocks = [] if window.entries_open(clock.now) else ["OUTSIDE_ENTRY_WINDOW"]
        async with sm() as s:
            held = sorted({p.symbol for p in await trading_repo.live_positions(s)})
        result = await decisions.cycle(calendar, universe, cfg, config_version, blocks,
                                       risk=risk, submit=submit, extra_symbols=held)
        if result is not None:
            report.proposals += len(result.proposals)
            for act in await exits.evaluate(bar_ts=result.bar_ts, bars=decisions.last_bars,
                                            contexts=decisions.contexts, session=session,
                                            regime=result.regime, cfg=cfg):
                say(f"{clock():%H:%M} {act.kind} {act.symbol} {act.detail}")
        await om.check_entry_timeouts(cfg.execution.entry_timeout_seconds)
        if clock.now >= window.force_close:
            await liquidator.eod_tick(clock.now, window)
        await sim.drain(om.on_trade_update)

    report.flat_at_close = await liquidator.is_flat()
    async with sm() as s:
        closed = list((await s.execute(select(Position).where(
            Position.session_date == day, Position.status == "CLOSED")
            .order_by(Position.entry_time))).scalars())
        daily = await s.get(DailyPnl, day)
    report.trades = [{"symbol": p.symbol, "strategy": p.strategy, "qty": int(
        (p.exit_detail or {}).get("exit_qty") or 0), "entry": str(p.avg_entry_price),
        "exit": str(p.avg_exit_price), "exit_reason": p.exit_reason, "net_pnl": str(p.net_pnl)}
        for p in closed]
    report.realized_net = Decimal(daily.realized_net) if daily else Decimal(0)
    return report
