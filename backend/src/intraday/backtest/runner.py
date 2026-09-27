"""Backtest simulator (SPEC §24; docs/design/08 §10, 12 Phase 6).

Replays stored sessions minute by minute through the same code as live:
signal pipeline (features → regime → strategies → score → proposals) → risk
engine (full R1-R26 with the simulated portfolio) → SimBroker fill model →
exit rules. No look-ahead: decisions at a bar's close only see bars up to that
bar, and they execute on the next bar.

Documented assumptions (reviewable):
- Entries are marketable limits valid for one bar (entry timeout < 1 minute).
- No quotes in history: the risk engine's spread check uses
  ``CostModel.assumed_spread_pct``.
- Correlation clusters are not modelled in backtests (sector limits are).
- The daily-loss latch is reset at the next session (live requires a manual
  reset).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from intraday.ai.pipeline import run_cycle
from intraday.backtest.costs import CostModel
from intraday.backtest.metrics import TradeResult
from intraday.core.assets import Eligibility
from intraday.core.runtime_config import TradingConfig
from intraday.features.feature_engineering import SymbolContext, compute_features
from intraday.features.regime import Regime
from intraday.marketdata.calendar import SessionDay, entry_window
from intraday.marketdata.types import BAR_INTERVAL, Bar
from intraday.risk.limits import effective_limits
from intraday.risk.models import (
    AccountSnapshot,
    Clock,
    GateSnapshot,
    MarketSnapshot,
    OpenPosition,
    PendingEntry,
    PortfolioSnapshot,
    RiskInputs,
    RiskProposal,
)
from intraday.risk.risk_manager import evaluate as risk_evaluate
from intraday.trading.broker.sim import check_bracket, fill_entry, fill_market_exit
from intraday.trading.exits import ExitSignal, bar_close_exit, trailing_stop

BPS = Decimal("10000")
CENT = Decimal("0.01")
EQUITY_SAMPLE_MINUTES = 5


@dataclass
class SessionData:
    session: SessionDay
    bars: dict[str, list[Bar]]              # today's regular-session bars (incl. SPY/QQQ)
    contexts: dict[str, SymbolContext]


@dataclass
class SimPosition:
    symbol: str
    strategy: str
    qty: int
    entry_price: Decimal
    entry_time: datetime
    initial_stop: Decimal
    stop: Decimal
    target: Decimal
    score: float
    sector: str | None
    high_water: Decimal
    entry_fees: Decimal
    trailing: bool = False
    exit_signal: ExitSignal | None = None
    last_close: Decimal = Decimal(0)


@dataclass
class PendingOrder:
    symbol: str
    strategy: str
    qty: int
    limit: Decimal
    stop: Decimal
    target: Decimal
    score: float
    sector: str | None


@dataclass
class Event:
    ts: datetime
    type: str
    symbol: str | None
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"ts": self.ts.isoformat(), "type": self.type, "symbol": self.symbol,
                "detail": self.detail}


@dataclass
class DayState:
    realized: Decimal = Decimal(0)
    trades: int = 0
    per_symbol: dict[str, int] = field(default_factory=dict)
    last_exit: dict[str, datetime] = field(default_factory=dict)
    locked: bool = False


class Simulator:
    def __init__(self, cfg: TradingConfig, costs: CostModel, starting_capital: Decimal, *,
                 feed: str, eligibility: Mapping[str, Eligibility],
                 sectors: Mapping[str, str | None], config_version: int = 0,
                 on_event: Callable[[Event], None] | None = None) -> None:
        self.cfg, self.costs, self.feed = cfg, costs, feed
        self.starting_capital = starting_capital
        self.cash = starting_capital
        self.eligibility, self.sectors = eligibility, sectors
        self.config_version = config_version
        self.positions: dict[str, SimPosition] = {}
        self.trades: list[TradeResult] = []
        self.curve: list[tuple[datetime, float]] = []
        self.daily_equity: list[tuple[date, float]] = []
        self.events: list[Event] = []
        self._on_event = on_event

    # ------------------------------------------------------------------ helpers

    def _emit(self, ts: datetime, type_: str, symbol: str | None, detail: str) -> None:
        ev = Event(ts, type_, symbol, detail)
        self.events.append(ev)
        if self._on_event:
            self._on_event(ev)

    def equity(self) -> Decimal:
        return self.cash + sum((p.last_close * p.qty for p in self.positions.values()),
                               Decimal(0))

    def _close(self, pos: SimPosition, price: Decimal, when: datetime, reason: str,
               day: DayState, session_date: date) -> None:
        fees = self.costs.sell_fees(pos.qty, price)
        self.cash += price * pos.qty - fees
        gross = ((price - pos.entry_price) * pos.qty).quantize(CENT, rounding=ROUND_HALF_UP)
        total_fees = pos.entry_fees + fees
        net = gross - total_fees
        day.realized += net
        day.last_exit[pos.symbol] = when
        self.trades.append(TradeResult(pos.symbol, pos.strategy, session_date, pos.entry_time,
                                       when, pos.qty, pos.entry_price, price, gross, total_fees,
                                       net, reason, pos.score))
        del self.positions[pos.symbol]
        self._emit(when, "EXIT", pos.symbol, f"{reason} {pos.qty} @ {price} net {net}")

    def _portfolio(self, pending: Sequence[PendingOrder], day: DayState) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            positions=tuple(OpenPosition(p.symbol, p.qty, p.entry_price, p.last_close, p.stop,
                                         p.sector) for p in self.positions.values()),
            pending=tuple(PendingEntry(o.symbol, o.qty, o.limit, o.stop, o.sector)
                          for o in pending),
            realized_pnl_today=day.realized, trades_today=day.trades,
            symbol_trades_today=dict(day.per_symbol), last_exit_at=dict(day.last_exit))

    # ------------------------------------------------------------------ session

    def run_session(self, sd: SessionData, universe: Sequence[str]) -> None:
        session, cfg = sd.session, self.cfg
        window = entry_window(session, cfg.hours)
        by_ts: dict[str, dict[datetime, Bar]] = {s: {b.ts: b for b in bs}
                                                 for s, bs in sd.bars.items()}
        minutes = sorted({b.ts for bs in sd.bars.values() for b in bs
                          if session.open_at <= b.ts < session.close_at})
        day = DayState()
        pending: list[PendingOrder] = []
        limits = effective_limits(cfg, self.starting_capital)
        slip = self.costs.slippage_bps
        seen: dict[str, list[Bar]] = {s: [] for s in sd.bars}

        for m in minutes:
            close_time = m + BAR_INTERVAL
            for s, bs in by_ts.items():
                if m in bs:
                    seen[s].append(bs[m])

            # 1. entries placed at the previous close fill (or expire) on this bar
            for order in pending:
                bar = by_ts.get(order.symbol, {}).get(m)
                fill = fill_entry(bar, order.limit, slip) if bar else None
                if bar is None or fill is None:
                    self._emit(m, "ENTRY_EXPIRED", order.symbol, f"limit {order.limit} unfilled")
                    continue
                fees = self.costs.buy_fees(order.qty, fill.price)
                self.cash -= fill.price * order.qty + fees
                pos = SimPosition(order.symbol, order.strategy, order.qty, fill.price, m,
                                  order.stop, order.stop, order.target, order.score,
                                  order.sector, fill.price, fees, last_close=bar.close)
                self.positions[order.symbol] = pos
                self._emit(m, "ENTRY", order.symbol,
                           f"{order.strategy} {order.qty} @ {fill.price} stop {order.stop} "
                           f"target {order.target}")
                hit = check_bracket(bar, pos.stop, pos.target, slip, entry_bar=True)
                if hit:
                    self._close(pos, hit.price, m, hit.reason, day, session.session_date)
            pending = []

            # 2. market exits decided at the previous close; 3. bracket legs; 4. trailing
            for sym, pos in list(self.positions.items()):
                bar = by_ts.get(sym, {}).get(m)
                if bar is None or pos.entry_time == m:
                    continue
                if pos.exit_signal is not None:
                    self._close(pos, fill_market_exit(bar, slip), m, pos.exit_signal.reason,
                                day, session.session_date)
                    continue
                hit = check_bracket(bar, pos.stop, pos.target, slip)
                if hit:
                    reason = "TRAILING_STOP" if hit.reason == "STOP_LOSS" and pos.trailing \
                        else hit.reason
                    self._close(pos, hit.price, m, reason, day, session.session_date)
                    continue
                pos.high_water = max(pos.high_water, bar.high)
                pos.last_close = bar.close
                new_stop, active = trailing_stop(entry=pos.entry_price,
                                                 initial_stop=pos.initial_stop,
                                                 current_stop=pos.stop,
                                                 high_water=pos.high_water, cfg=cfg.exits)
                if new_stop > pos.stop:
                    self._emit(m, "STOP_RAISED", sym, f"{pos.stop} -> {new_stop}")
                pos.stop, pos.trailing = new_stop, pos.trailing or active
            for sym, pos in self.positions.items():
                bar = by_ts.get(sym, {}).get(m)
                if bar is not None:
                    pos.last_close = bar.close

            # 5. bar close: pipeline, exit rules, daily loss, entries
            cycle = run_cycle(m, seen, sd.contexts, session, universe, cfg, self.config_version)
            self._exit_rules(m, close_time, cycle.regime, seen, sd, window.force_close)

            daily_pnl = day.realized + sum(((p.last_close - p.entry_price) * p.qty
                                            for p in self.positions.values()), Decimal(0))
            if not day.locked and -daily_pnl >= limits["max_daily_loss"]:
                day.locked = True
                self._emit(close_time, "DAILY_LOSS_LIMIT", None, f"daily P&L {daily_pnl}")
                if cfg.risk.daily_loss_action == "FLATTEN":
                    for pos in self.positions.values():
                        pos.exit_signal = pos.exit_signal or ExitSignal(
                            "DAILY_LOSS_FLATTEN", "daily loss limit reached")

            for rec in sorted(cycle.proposals, key=lambda r: -r.score):
                new = self._consider(rec, close_time, window, cycle.regime, pending, day)
                if new is not None:
                    pending.append(new)
                    day.trades += 1
                    day.per_symbol[new.symbol] = day.per_symbol.get(new.symbol, 0) + 1

            if (m - session.open_at) % timedelta(minutes=EQUITY_SAMPLE_MINUTES) == timedelta(0):
                self.curve.append((close_time, float(self.equity())))

        # Safety net: anything still open at the end of the data is liquidated at its last close.
        last = minutes[-1] + BAR_INTERVAL if minutes else session.close_at
        for pos in list(self.positions.values()):
            price = (pos.last_close * (1 - Decimal(slip) / BPS)).quantize(Decimal("0.0001"))
            self._close(pos, price, last, "EOD_LIQUIDATION", day, session.session_date)
        for o in pending:
            self._emit(last, "ENTRY_EXPIRED", o.symbol, "session ended")
        self.curve.append((last, float(self.equity())))
        self.daily_equity.append((session.session_date, float(self.equity())))

    def _exit_rules(self, m: datetime, close_time: datetime, regime: Regime,
                    seen: Mapping[str, list[Bar]], sd: SessionData, force_close: datetime
                    ) -> None:
        for sym, pos in self.positions.items():
            if pos.exit_signal is not None or not seen.get(sym) or seen[sym][-1].ts != m:
                continue
            state = compute_features(seen[sym], sd.contexts.get(sym, SymbolContext(sym)),
                                     sd.session)
            if state is None:
                continue
            below = 0
            for c, v in zip(reversed(state.series.closes), reversed(state.series.vwaps),
                            strict=True):
                if c < v:
                    below += 1
                else:
                    break
            sig = bar_close_exit(f=state.features, entry_time=pos.entry_time, now=close_time,
                                 cfg=self.cfg.exits, regime=regime, closes_below_vwap=below,
                                 force_close=force_close)
            if sig is not None:
                pos.exit_signal = sig
                self._emit(close_time, "EXIT_SIGNAL", sym, f"{sig.reason}: {sig.detail}")
        # Symbols that don't trade at the force-close minute still must be flattened.
        if close_time >= force_close:
            for pos in self.positions.values():
                pos.exit_signal = pos.exit_signal or ExitSignal("EOD_LIQUIDATION",
                                                                "force-close time reached")

    def _consider(self, rec: Any, now: datetime, window: Any, regime: Regime,
                  pending: Sequence[PendingOrder], day: DayState) -> PendingOrder | None:
        p = rec.proposal
        sym = rec.symbol
        entry = (p.entry_ref_price * (1 + Decimal(self.cfg.execution.entry_limit_offset_bps)
                                      / BPS)).quantize(CENT, rounding=ROUND_HALF_UP)
        equity = self.equity()
        inputs = RiskInputs(
            proposal=RiskProposal(sym, "BUY", entry, p.stop_loss, p.take_profit),
            portfolio=self._portfolio(pending, day),
            account=AccountSnapshot(equity, self.cash, max(self.cash, Decimal(0))),
            market=MarketSnapshot(sym, p.entry_ref_price, self.costs.assumed_spread_pct, (),
                                  self.eligibility.get(sym, _INELIGIBLE),
                                  self.sectors.get(sym)),
            gate=GateSnapshot(("DAILY_LOSS_LIMIT",) if day.locked else (), True),
            clock=Clock(now, True, window), regime=regime, feed=self.feed)
        d = risk_evaluate(inputs, self.cfg)
        if not d.approved:
            self._emit(now, "RISK_REJECTED", sym, f"{rec.strategy}: " + ", ".join(
                f"{c.id} {c.name}" for c in d.checks if not c.passed))
            return None
        self._emit(now, "ENTRY_ORDER", sym, f"{rec.strategy} score {rec.score:.2f} "
                                            f"{d.qty} @ <={entry} (binding {d.sizing.binding})")
        return PendingOrder(sym, rec.strategy, d.qty, entry, p.stop_loss, p.take_profit,
                            rec.score, self.sectors.get(sym))


def _ineligible() -> Eligibility:
    from intraday.core.assets import RuleResult
    return Eligibility({"E1": RuleResult(False, "not in the backtest's eligible universe")})


_INELIGIBLE = _ineligible()
