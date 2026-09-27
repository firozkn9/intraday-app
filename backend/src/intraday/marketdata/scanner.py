"""Universe scanner (engine component, Phase 3).

Runs the universe filters for every TRADABLE watchlist symbol and records one
``universe_scans`` row per symbol with every filter's reason. Inputs come from
the database (daily bars, today's 1-min bars, watchlist/asset cache) plus live
latest quotes during the session. Context symbols are never scanned.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.logging import get_logger
from intraday.core.runtime_config import TradingConfig
from intraday.core.timeutil import ET, utcnow
from intraday.db.repositories import marketdata as md_repo
from intraday.db.session import transaction
from intraday.marketdata.alpaca.http import AlpacaError
from intraday.marketdata.service import MarketDataService
from intraday.marketdata.types import Bar, Quote
from intraday.marketdata.universe import DailyVolume, ScanInputs, ScanResult, evaluate
from intraday.marketdata.watchlist import eligibility_of, manually_plausible

log = get_logger(__name__)

RECENT_WINDOW = timedelta(minutes=35)


@dataclass
class ScanSummary:
    scan_id: str
    ts: datetime
    session_date: str
    passed: list[str] = field(default_factory=list)
    failed: dict[str, list[str]] = field(default_factory=dict)
    quotes_error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"scan_id": self.scan_id, "ts": self.ts.isoformat(),
                "session_date": self.session_date, "passed": self.passed,
                "failed": self.failed, "quotes_error": self.quotes_error}


class UniverseScanner:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession],
                 market: MarketDataService, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.sessionmaker = sessionmaker
        self.market = market
        self.clock = clock
        self.last: ScanSummary | None = None
        self.results: dict[str, ScanResult] = {}

    @property
    def passed(self) -> frozenset[str]:
        return frozenset(s for s, r in self.results.items() if r.passed)

    def due(self, now: datetime, interval_minutes: int) -> bool:
        return self.last is None or now - self.last.ts >= timedelta(minutes=interval_minutes)

    async def run(self, cfg: TradingConfig, config_version: int) -> ScanSummary:
        now = self.clock()
        feed = self.market.feed
        today = now.astimezone(ET).date()
        calendar = self.market.calendar
        session = calendar.session(today)
        is_open = calendar.is_open(now)

        async with self.sessionmaker() as s:
            rows = [r for r in await md_repo.list_symbols(s) if r.role == "TRADABLE"]
            symbols = [r.symbol for r in rows]
            # Generous date range; average_daily_volume() keeps the most recent N sessions.
            # (Deliberately independent of how far back the cached calendar reaches.)
            lookback = cfg.universe.adv_lookback_days
            start = today - timedelta(days=lookback * 2 + 14)
            daily = await md_repo.daily_bars_between(s, feed, symbols, start, today)
            recent = (await md_repo.bars_since(s, feed, symbols,
                                               max(session.open_at, now - RECENT_WINDOW)))\
                if session is not None else []

        by_daily: dict[str, list[DailyVolume]] = defaultdict(list)
        for d in daily:
            by_daily[d.symbol].append(DailyVolume(d.session_date, d.volume, d.close))
        by_recent: dict[str, list[Bar]] = defaultdict(list)
        for b in recent:
            by_recent[b.symbol].append(Bar(b.symbol, b.feed, b.ts, b.open, b.high, b.low,
                                           b.close, b.volume, b.vwap, b.trade_count))

        quotes: dict[str, Quote] = {}
        quotes_error = None
        quotable = [r.symbol for r in rows if r.enabled and manually_plausible(r)]
        if is_open and quotable:
            try:
                quotes = await self.market.data.get_latest_quotes(quotable, feed)
            except (AlpacaError, httpx.HTTPError, OSError) as exc:
                quotes_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("scan_quotes_failed", error=quotes_error)

        results: dict[str, ScanResult] = {}
        for row in rows:
            inputs = ScanInputs(
                symbol=row.symbol, enabled=row.enabled, eligibility=eligibility_of(row, now),
                market_cap=Decimal(row.market_cap) if row.market_cap is not None else None,
                daily=by_daily.get(row.symbol, []), recent_bars=by_recent.get(row.symbol, []),
                quote=quotes.get(row.symbol))
            results[row.symbol] = evaluate(inputs, cfg.universe, feed=feed, now=now,
                                           session=session)

        scan_id = md_repo.new_scan_id()
        async with transaction(self.sessionmaker) as s:
            await md_repo.insert_scan(s, [
                {"scan_id": scan_id, "ts": now, "session_date": today, "symbol": sym,
                 "passed": r.passed, "reasons": r.reasons_json(), "metrics": r.metrics,
                 "feed": feed, "config_version": config_version}
                for sym, r in results.items()])

        self.results = results
        summary = ScanSummary(str(scan_id), now, today.isoformat(),
                              passed=sorted(s for s, r in results.items() if r.passed),
                              failed={s: r.failed for s, r in results.items() if not r.passed},
                              quotes_error=quotes_error)
        self.last = summary
        log.info("universe_scanned", passed=len(summary.passed), failed=len(summary.failed),
                 market_open=is_open)
        return summary
