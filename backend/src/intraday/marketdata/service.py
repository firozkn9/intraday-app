"""Market-data service (engine component, Phase 2).

- Calendar: Alpaca /v2/calendar → market_calendar table (DB is the fallback).
- Assets: Alpaca /v2/assets → symbols.alpaca_* + eligibility (E1-E7).
- Bars: incremental REST backfill (1-min regular-session bars + daily bars),
  real-time stream, and a gap-fill backfill after every (re)connect.
- Health: market-wide staleness feeds the DATA_STALE gate block.

Only regular-session 1-minute bars are stored: historical requests also return
pre/after-market bars, which the strategies don't use.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.assets import AlpacaAsset, evaluate
from intraday.core.logging import get_logger
from intraday.core.timeutil import ET, utcnow
from intraday.db.repositories import marketdata as md_repo
from intraday.db.session import transaction
from intraday.marketdata.alpaca.clients import AlpacaDataClient, AlpacaReferenceClient
from intraday.marketdata.alpaca.stream import AlpacaBarStream, StreamEvent
from intraday.marketdata.calendar import MarketCalendar, SessionDay
from intraday.marketdata.quality import DataHealth, assess, in_regular_session
from intraday.marketdata.types import BAR_INTERVAL, Bar, DailyBar
from intraday.marketdata.watchlist import StreamPlan, meta_of, stream_plan

log = get_logger(__name__)

CALENDAR_DAYS_BACK = 120
CALENDAR_DAYS_FORWARD = 60
DAILY_HISTORY_CALENDAR_DAYS = 100   # ≥ 60 sessions for ADV/ATR lookbacks
MAX_BACKFILL_DAYS = 60
DEFAULT_BACKFILL_DAYS = 30
SIP_FREE_PLAN_DELAY = timedelta(minutes=16)  # free plan: no SIP data from the last 15 min
ASSET_RESYNC_AFTER = timedelta(hours=20)


@dataclass
class BackfillReport:
    started_at: datetime
    finished_at: datetime | None = None
    minute_bars: int = 0
    daily_bars: int = 0
    skipped_outside_session: int = 0
    symbols: int = 0
    requests: list[str] = field(default_factory=list)
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "minute_bars": self.minute_bars, "daily_bars": self.daily_bars,
                "skipped_outside_session": self.skipped_outside_session,
                "symbols": self.symbols, "error": self.error}


class MarketDataService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], data: AlpacaDataClient,
                 reference: AlpacaReferenceClient, stream: AlpacaBarStream, *, feed: str,
                 stream_limit: int, backfill_days: int = DEFAULT_BACKFILL_DAYS,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.sessionmaker = sessionmaker
        self.data, self.reference, self.stream = data, reference, stream
        self.feed = feed
        self.stream_limit = stream_limit
        self.backfill_days = backfill_days
        self.clock = clock
        self.calendar = MarketCalendar([])
        self.latest: dict[str, Bar] = {}
        self.plan = StreamPlan([], [], stream_limit)
        self.last_backfill: BackfillReport | None = None
        self._backfill_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ calendar

    async def load_calendar(self) -> MarketCalendar:
        today = self.clock().astimezone(ET).date()
        async with self.sessionmaker() as s:
            self.calendar = await md_repo.load_calendar(
                s, today - timedelta(days=CALENDAR_DAYS_BACK),
                today + timedelta(days=CALENDAR_DAYS_FORWARD))
        return self.calendar

    async def sync_calendar(self) -> int:
        today = self.clock().astimezone(ET).date()
        raw = await self.reference.get_calendar(today - timedelta(days=CALENDAR_DAYS_BACK),
                                                today + timedelta(days=CALENDAR_DAYS_FORWARD))
        days = [SessionDay.from_alpaca(r) for r in raw]
        async with transaction(self.sessionmaker) as s:
            n = await md_repo.upsert_calendar(s, days)
        await self.load_calendar()
        log.info("calendar_synced", sessions=n, until=str(self.calendar.last))
        return n

    # ------------------------------------------------------------------ watchlist & assets

    async def refresh_watchlist(self) -> StreamPlan:
        async with self.sessionmaker() as s:
            rows = await md_repo.list_symbols(s)
        plan = stream_plan(rows, self.stream_limit)
        if plan.dropped and plan.dropped != self.plan.dropped:
            log.warning("stream_budget_exceeded", limit=self.stream_limit, dropped=plan.dropped)
        if plan.subscribed != self.plan.subscribed:
            await self.stream.set_symbols(plan.subscribed)
        self.plan = plan
        return plan

    async def sync_assets(self, *, force: bool = False) -> dict[str, bool]:
        """Refresh Alpaca asset data and eligibility. Returns symbol → eligible."""
        now = self.clock()
        async with self.sessionmaker() as s:
            rows = await md_repo.list_symbols(s, enabled_only=True)
        out: dict[str, bool] = {}
        for row in rows:
            fresh = row.alpaca_synced_at and now - row.alpaca_synced_at < ASSET_RESYNC_AFTER
            if fresh and not force:
                continue
            raw = await self.reference.get_asset(row.symbol)
            values: dict[str, Any] = {"alpaca_synced_at": now}
            if raw is None:
                values.update(alpaca_status="not_found", alpaca_tradable=False,
                              alpaca_class=None, alpaca_exchange=None, alpaca_name=None)
                asset = AlpacaAsset(row.symbol, "", "", "", "not_found", False, now)
            else:
                asset = AlpacaAsset.from_api(raw, now)
                values.update(alpaca_class=asset.asset_class, alpaca_exchange=asset.exchange,
                              alpaca_name=asset.name, alpaca_status=asset.status,
                              alpaca_tradable=asset.tradable)
            elig = evaluate(meta_of(row), asset, now)
            values["eligibility"] = elig.to_json()
            async with transaction(self.sessionmaker) as s:
                await md_repo.update_symbol(s, row.symbol, **values)
            out[row.symbol] = elig.eligible
        if out:
            log.info("assets_synced", count=len(out),
                     ineligible_tradable=[k for k, v in out.items() if not v])
        return out

    # ------------------------------------------------------------------ backfill

    def _backfill_end(self, now: datetime) -> datetime:
        return now - SIP_FREE_PLAN_DELAY if self.feed == "sip" else now

    async def backfill(self, days: int | None = None, symbols: Sequence[str] | None = None,
                       *, full: bool = False) -> BackfillReport:
        """Incremental by default: each symbol resumes after its latest stored bar."""
        days = max(1, min(days or self.backfill_days, MAX_BACKFILL_DAYS))
        async with self._backfill_lock:
            report = BackfillReport(started_at=self.clock())
            try:
                await self._backfill(report, days, symbols, full)
            except Exception as exc:
                report.error = f"{type(exc).__name__}: {exc}"[:500]
                log.error("backfill_failed", error=report.error)
                raise
            finally:
                report.finished_at = self.clock()
                self.last_backfill = report
            log.info("backfill_done", **report.to_json())
            return report

    async def _backfill(self, report: BackfillReport, days: int,
                        symbols: Sequence[str] | None, full: bool) -> None:
        now = self.clock()
        if not self.calendar.covers(now.astimezone(ET).date()):
            await self.sync_calendar()
        symbols = list(symbols if symbols is not None else self.plan.subscribed)
        report.symbols = len(symbols)
        sessions = self.calendar.last_n_sessions(days, now)
        end = self._backfill_end(now)
        if not symbols or not sessions:
            return
        window_start = sessions[0].open_at

        async with self.sessionmaker() as s:
            latest = {} if full else await md_repo.latest_bar_ts(s, self.feed, symbols)
        groups: dict[datetime, list[str]] = {}
        for sym in symbols:
            last = latest.get(sym)
            start = window_start if last is None else max(window_start, last + BAR_INTERVAL)
            if start < end:
                groups.setdefault(start, []).append(sym)

        for start, group in sorted(groups.items()):
            raw = await self.data.get_bars(group, "1Min", start, end, self.feed)
            report.requests.append(f"1Min {len(group)} symbols from {start.isoformat()}")
            bars: list[Bar] = []
            for sym, items in raw.items():
                for item in items:
                    bar = Bar.from_alpaca(sym, self.feed, item)
                    if in_regular_session(bar, self.calendar):
                        bars.append(bar)
                    else:
                        report.skipped_outside_session += 1
            async with transaction(self.sessionmaker) as s:
                report.minute_bars += await md_repo.upsert_bars(s, bars)
            self._remember(bars)

        daily_start = now - timedelta(days=DAILY_HISTORY_CALENDAR_DAYS + days)
        raw_daily = await self.data.get_bars(symbols, "1Day", daily_start, end, self.feed)
        daily = [DailyBar.from_alpaca(sym, self.feed, item)
                 for sym, items in raw_daily.items() for item in items]
        async with transaction(self.sessionmaker) as s:
            report.daily_bars = await md_repo.upsert_daily_bars(s, daily)

    def _remember(self, bars: Sequence[Bar]) -> None:
        for bar in bars:
            cur = self.latest.get(bar.symbol)
            if cur is None or bar.ts >= cur.ts:
                self.latest[bar.symbol] = bar

    async def load_latest_from_db(self) -> None:
        """Latest stored bar per subscribed symbol (for staleness after a restart)."""
        symbols = self.plan.subscribed
        async with self.sessionmaker() as s:
            latest = await md_repo.latest_bar_ts(s, self.feed, symbols)
            for sym, ts in latest.items():
                rows = await md_repo.get_bars(s, sym, self.feed, ts, ts + BAR_INTERVAL, limit=1)
                if rows:
                    r = rows[0]
                    self._remember([Bar(r.symbol, r.feed, r.ts, r.open, r.high, r.low, r.close,
                                        r.volume, r.vwap, r.trade_count)])

    # ------------------------------------------------------------------ stream

    async def on_stream_event(self, ev: StreamEvent) -> None:
        if ev.kind in ("bar", "updated_bar") and ev.bar is not None:
            if not in_regular_session(ev.bar, self.calendar):
                return
            async with transaction(self.sessionmaker) as s:
                await md_repo.upsert_bars(s, [ev.bar])
            self._remember([ev.bar])
        elif ev.kind == "connected":
            # Gap-fill whatever the stream missed while disconnected (or before boot).
            self._spawn(self._gap_fill(), "gap_fill")

    async def _gap_fill(self) -> None:
        try:
            await self.backfill()
        except Exception as exc:
            log.warning("gap_fill_failed", error=str(exc))

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.data.http.aclose()
        await self.reference.http.aclose()

    async def wait_idle(self) -> None:
        """Wait for background tasks (tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------ health

    def health(self, now: datetime, stale_seconds: int) -> DataHealth:
        return assess(self.latest, self.plan.subscribed, self.calendar, now, stale_seconds,
                      stream_connected=self.stream.stats.state == "connected")

    def snapshot(self, now: datetime, stale_seconds: int) -> dict[str, Any]:
        return {"enabled": True, "feed": self.feed, "stream": self.stream.stats.to_json(),
                "plan": self.plan.to_json(),
                "health": self.health(now, stale_seconds).to_json(),
                "last_backfill": self.last_backfill.to_json() if self.last_backfill else None}
