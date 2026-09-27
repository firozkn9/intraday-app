"""Backtest jobs: parameters, data loading from the database, execution, persistence.

Each split (TRAIN / VALIDATION / TEST) runs independently from the starting
capital. Nothing is optimised here: the splits exist so results can be judged
out-of-sample (SPEC §24). Data is loaded one session at a time with a rolling
window of prior sessions for context (volume profiles, warm-up), so memory stays
bounded. The same feed as live is used (CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.backtest.costs import CostModel
from intraday.backtest.metrics import summarize
from intraday.backtest.runner import Event, SessionData, Simulator
from intraday.core.assets import Eligibility, evaluate
from intraday.core.logging import get_logger
from intraday.core.runtime_config import TradingConfig, parse_config
from intraday.core.timeutil import ET, utcnow
from intraday.db.models import BacktestEquity, BacktestRun, BacktestTrade, Symbol
from intraday.db.repositories import marketdata as md_repo
from intraday.db.repositories import signals as sig_repo
from intraday.db.session import transaction
from intraday.features.feature_engineering import DailyStat, build_context
from intraday.features.regime import REFERENCE
from intraday.marketdata.calendar import MarketCalendar, SessionDay
from intraday.marketdata.types import BAR_INTERVAL, Bar
from intraday.marketdata.watchlist import asset_of, meta_of

log = get_logger(__name__)

SPLITS = ("TRAIN", "VALIDATION", "TEST")
HISTORY_SESSIONS = 20
MAX_SESSIONS_PER_RUN = 300


class DateRange(BaseModel):
    model_config = ConfigDict(frozen=True)
    start: date
    end: date

    @model_validator(mode="after")
    def _order(self) -> DateRange:
        if self.end < self.start:
            raise ValueError("end must not be before start")
        return self


class BacktestParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    train: DateRange | None = None
    validation: DateRange | None = None
    test: DateRange | None = None
    symbols: list[str] | None = Field(None, max_length=40)
    strategies: list[Literal["momentum_breakout", "vwap_reclaim", "pullback"]] | None = None
    starting_capital: Decimal | None = Field(None, ge=1000, le=10_000_000)
    costs: CostModel = CostModel()
    config_overrides: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _splits(self) -> BacktestParams:
        ranges = [(n, r) for n, r in self.split_ranges()]
        if not ranges:
            raise ValueError("give at least one of train / validation / test")
        for (a, ra), (b, rb) in pairwise(ranges):
            if rb.start <= ra.end:
                raise ValueError(f"{b.lower()} must start after {a.lower()} ends "
                                 "(splits are sequential and non-overlapping)")
        return self

    def split_ranges(self) -> list[tuple[str, DateRange]]:
        return [(n, r) for n, r in (("TRAIN", self.train), ("VALIDATION", self.validation),
                                    ("TEST", self.test)) if r is not None]


def effective_config(base: TradingConfig, params: BacktestParams) -> TradingConfig:
    data = base.to_json()
    for group, values in params.config_overrides.items():
        if isinstance(values, dict) and isinstance(data.get(group), dict):
            data[group] = _merge(data[group], values)
        else:
            data[group] = values
    cfg = parse_config(data)
    if params.strategies is not None:
        strat = cfg.strategies.model_copy(update={
            name: getattr(cfg.strategies, name).model_copy(
                update={"enabled": name in params.strategies})
            for name in ("momentum_breakout", "vwap_reclaim", "pullback")})
        cfg = cfg.model_copy(update={"strategies": strat})
    return cfg


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


# ------------------------------------------------------------------ data loading

async def load_calendar(sm: async_sessionmaker[AsyncSession], start: date, end: date
                        ) -> MarketCalendar:
    async with sm() as s:
        return await md_repo.load_calendar(s, start, end)


async def coverage_gaps(sm: async_sessionmaker[AsyncSession], params: BacktestParams, feed: str
                        ) -> list[str]:
    """Problems that make the requested ranges unusable (calendar or bars missing)."""
    first = min(r.start for _, r in params.split_ranges())
    last = max(r.end for _, r in params.split_ranges())
    cal = await load_calendar(sm, first, last)
    problems = []
    for name, r in params.split_ranges():
        sessions = cal.sessions_between(r.start, r.end)
        if not sessions:
            problems.append(f"{name}: no market-calendar sessions between {r.start} and {r.end}"
                            " (sync the calendar / run a backfill)")
            continue
        if len(sessions) > MAX_SESSIONS_PER_RUN:
            problems.append(f"{name}: {len(sessions)} sessions exceeds {MAX_SESSIONS_PER_RUN}")
        async with sm() as s:
            rows = await sig_repo.minute_bars(s, feed, ["SPY"], sessions[0].open_at,
                                              sessions[-1].close_at)
        have = {r.ts.astimezone(ET).date() for r in rows}
        missing = [str(x.session_date) for x in sessions if x.session_date not in have]
        if missing:
            shown = ", ".join(missing[:10]) + (" …" if len(missing) > 10 else "")
            problems.append(f"{name}: no {feed.upper()} bars for {len(missing)} session(s): "
                            f"{shown} (run a backfill)")
    return problems


def _bar(r: Any) -> Bar:
    return Bar(r.symbol, r.feed, r.ts, r.open, r.high, r.low, r.close, r.volume, r.vwap,
               r.trade_count)


async def iter_sessions(sm: async_sessionmaker[AsyncSession], feed: str, cal: MarketCalendar,
                        sessions: Sequence[SessionDay], symbols: Sequence[str],
                        adv_lookback: int) -> AsyncIterator[SessionData]:
    """Yield SessionData one session at a time with a rolling context window."""
    all_syms = sorted(set(symbols) | set(REFERENCE))
    first = sessions[0]
    prior = [s for s in cal.sessions_between(first.session_date - timedelta(days=45),
                                             first.session_date - timedelta(days=1))]
    prior = prior[-HISTORY_SESSIONS:]
    async with sm() as s:
        daily = await sig_repo.daily_stats(s, feed, all_syms,
                                           first.session_date - timedelta(days=90),
                                           sessions[-1].session_date + timedelta(days=1))
    daily_by: dict[str, list[DailyStat]] = defaultdict(list)
    for d in daily:
        daily_by[d.symbol].append(DailyStat(d.session_date, float(d.high), float(d.low),
                                            float(d.close), float(d.volume)))

    window: deque[tuple[SessionDay, dict[str, list[Bar]]]] = deque(maxlen=HISTORY_SESSIONS)
    for p in prior:
        window.append((p, await _session_bars(sm, feed, p, all_syms)))

    for sess in sessions:
        today = await _session_bars(sm, feed, sess, all_syms)
        contexts = {}
        for sym in all_syms:
            hist = {p.session_date: (p, bars.get(sym, [])) for p, bars in window}
            warm = window[-1][1].get(sym, []) if window else []
            contexts[sym] = build_context(
                sym, [d for d in daily_by.get(sym, []) if d.session_date < sess.session_date],
                hist, warm, adv_lookback=adv_lookback)
        yield SessionData(sess, today, contexts)
        window.append((sess, today))


async def _session_bars(sm: async_sessionmaker[AsyncSession], feed: str, sess: SessionDay,
                        symbols: Sequence[str]) -> dict[str, list[Bar]]:
    async with sm() as s:
        rows = await sig_repo.minute_bars(s, feed, symbols, sess.open_at,
                                          sess.close_at - BAR_INTERVAL)
    out: dict[str, list[Bar]] = defaultdict(list)
    for r in rows:
        out[r.symbol].append(_bar(r))
    return dict(out)


async def universe_info(sm: async_sessionmaker[AsyncSession], symbols: Sequence[str] | None
                        ) -> tuple[list[str], dict[str, Eligibility], dict[str, str | None]]:
    """Symbols (default: enabled tradable watchlist), eligibility and sectors.

    Eligibility uses the cached Alpaca data at its sync time, so a backtest isn't
    blocked by the live 7-day freshness rule; unsynced symbols stay ineligible.
    """
    async with sm() as s:
        rows = list((await s.execute(select(Symbol).where(Symbol.role == "TRADABLE"))).scalars())
    by = {r.symbol: r for r in rows}
    chosen = [x.upper() for x in symbols] if symbols else sorted(
        r.symbol for r in rows if r.enabled)
    elig, sectors = {}, {}
    for sym in chosen:
        row = by.get(sym)
        if row is None:
            continue
        asset = asset_of(row)
        elig[sym] = evaluate(meta_of(row), asset, asset.synced_at if asset else utcnow())
        sectors[sym] = row.sector
    return chosen, elig, sectors


# ------------------------------------------------------------------ execution

async def run_backtest(sm: async_sessionmaker[AsyncSession], params: BacktestParams,
                       cfg: TradingConfig, *, feed: str, config_version: int = 0,
                       on_event: Callable[[Event], None] | None = None,
                       should_stop: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Run every split; returns {split: {"metrics", "trades", "curve"}}."""
    symbols, elig, sectors = await universe_info(sm, params.symbols)
    capital = params.starting_capital or cfg.risk.paper_initial_balance
    first = min(r.start for _, r in params.split_ranges())
    last = max(r.end for _, r in params.split_ranges())
    cal = await load_calendar(sm, first - timedelta(days=60), last)
    results: dict[str, Any] = {}
    for name, r in params.split_ranges():
        sessions = cal.sessions_between(r.start, r.end)
        sim = Simulator(cfg, params.costs, capital, feed=feed, eligibility=elig,
                        sectors=sectors, config_version=config_version, on_event=on_event)
        async for sd in iter_sessions(sm, feed, cal, sessions, symbols,
                                      cfg.universe.adv_lookback_days):
            if should_stop is not None and await _maybe_await(should_stop()):
                raise asyncio.CancelledError("backtest canceled")
            sim.run_session(sd, symbols)
            await asyncio.sleep(0)
        metrics = summarize(sim.trades, sim.daily_equity, float(capital),
                            [e for _, e in sim.curve])
        metrics["symbols"] = symbols
        results[name] = {"metrics": metrics, "trades": sim.trades, "curve": sim.curve,
                         "events": sim.events}
        log.info("backtest_split_done", split=name, sessions=len(sessions),
                 trades=len(sim.trades), total_return_pct=metrics["total_return_pct"])
    return results


async def _maybe_await(x: Any) -> Any:
    return await x if asyncio.iscoroutine(x) else x


async def execute_run(sm: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> None:
    """Run a persisted backtest (QUEUED → RUNNING → DONE / FAILED / CANCELED)."""
    async with transaction(sm) as s:
        run = await s.get(BacktestRun, run_id)
        if run is None or run.status != "QUEUED":
            return
        run.status, run.started_at = "RUNNING", utcnow()
        params = BacktestParams.model_validate(run.params)
        cfg = parse_config(run.config_snapshot)
        feed = run.feed

    async def canceled() -> bool:
        async with sm() as s:
            st = (await s.execute(select(BacktestRun.status).where(
                BacktestRun.id == run_id))).scalar_one()
        return st == "CANCELED"

    try:
        results = await run_backtest(sm, params, cfg, feed=feed, should_stop=canceled)
    except asyncio.CancelledError:
        async with transaction(sm) as s:
            await s.execute(update(BacktestRun).where(BacktestRun.id == run_id)
                            .values(finished_at=utcnow()))
        return
    except Exception as exc:  # any failure is recorded on the run
        log.exception("backtest_failed", run_id=str(run_id))
        async with transaction(sm) as s:
            await s.execute(update(BacktestRun).where(BacktestRun.id == run_id).values(
                status="FAILED", error=f"{type(exc).__name__}: {exc}"[:1000],
                finished_at=utcnow()))
        return

    async with transaction(sm) as s:
        for split, res in results.items():
            for t in res["trades"]:
                s.add(BacktestTrade(
                    run_id=run_id, split=split, session_date=t.session_date, symbol=t.symbol,
                    strategy=t.strategy, qty=t.qty, avg_entry_price=t.entry_price,
                    entry_time=t.entry_time, avg_exit_price=t.exit_price,
                    exit_time=t.exit_time, exit_reason=t.exit_reason,
                    score=Decimal(str(round(t.score, 4))), gross_pnl=t.gross_pnl, fees=t.fees,
                    net_pnl=t.net_pnl, return_pct=t.return_pct.quantize(Decimal("0.000001")),
                    holding_seconds=t.holding_seconds))
            peak = float("-inf")
            seen: set[datetime] = set()
            for ts, eq in res["curve"]:
                if ts in seen:
                    continue
                seen.add(ts)
                peak = max(peak, eq)
                s.add(BacktestEquity(run_id=run_id, ts=ts, equity=Decimal(str(round(eq, 2))),
                                     drawdown=Decimal(str(round(peak - eq, 2)))))
        await s.execute(update(BacktestRun).where(BacktestRun.id == run_id).values(
            status="DONE", finished_at=utcnow(),
            metrics={k: v["metrics"] for k, v in results.items()}))


def run_in_process(db_url: str, run_id: str) -> None:
    """Entry point for the API's worker process (keeps CPU work off the API's event loop)."""
    from intraday.db.session import make_engine, make_sessionmaker

    async def main() -> None:
        engine = make_engine(db_url, pool_size=2)
        try:
            await execute_run(make_sessionmaker(engine), uuid.UUID(run_id))
        finally:
            await engine.dispose()

    asyncio.run(main())
