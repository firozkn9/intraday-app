"""Market-data quality checks (docs/design/01 §6, 09 §2 "Data quality").

Pure functions over bars and the calendar. IEX only emits a 1-minute bar when
IEX printed a trade, so a missing bar means "no data", never "zero volume".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from intraday.core.timeutil import ET
from intraday.marketdata.calendar import MarketCalendar, SessionDay
from intraday.marketdata.types import BAR_INTERVAL, Bar

MARKET_REFERENCE_SYMBOLS = ("SPY", "QQQ")
COVERAGE_WINDOW = timedelta(minutes=30)
WARMUP_AFTER_OPEN = timedelta(minutes=2)
SUSPICIOUS_JUMP_PCT = Decimal("10")


def bar_age(bar: Bar | None, now: datetime) -> float | None:
    """Seconds since the bar *closed* (bars are stamped with their start time)."""
    return None if bar is None else (now - bar.end).total_seconds()


def coverage(bar_times: Sequence[datetime], session: SessionDay, now: datetime,
             window: timedelta = COVERAGE_WINDOW) -> float:
    """Fraction of completed minutes in the trailing window (within the session) with a bar."""
    end = min(now, session.close_at).replace(second=0, microsecond=0)
    start = max(session.open_at, end - window)
    expected = int((end - start) / BAR_INTERVAL)
    if expected <= 0:
        return 1.0
    have = sum(1 for t in bar_times if start <= t < end)
    return min(1.0, have / expected)


def bar_problems(bar: Bar, previous_close: Decimal | None = None) -> list[str]:
    problems = []
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        problems.append("non-positive price")
    if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
        problems.append("inconsistent OHLC")
    if bar.volume < 0:
        problems.append("negative volume")
    if previous_close and previous_close > 0:
        jump = abs(bar.close - previous_close) / previous_close * 100
        if jump >= SUSPICIOUS_JUMP_PCT:
            problems.append(f"price jump {jump:.1f}% vs previous close")
    return problems


def in_regular_session(bar: Bar, calendar: MarketCalendar) -> bool:
    session = calendar.session(bar.ts.astimezone(ET).date())
    return session is not None and session.open_at <= bar.ts < session.close_at


@dataclass
class DataHealth:
    market_open: bool
    stale: bool
    reasons: list[str] = field(default_factory=list)
    symbol_age_s: dict[str, float | None] = field(default_factory=dict)
    stale_symbols: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"market_open": self.market_open, "stale": self.stale, "reasons": self.reasons,
                "stale_symbols": self.stale_symbols,
                "reference_age_s": {s: self.symbol_age_s.get(s)
                                    for s in MARKET_REFERENCE_SYMBOLS}}


def assess(latest: Mapping[str, Bar], subscribed: Sequence[str], calendar: MarketCalendar,
           now: datetime, stale_seconds: int, *, stream_connected: bool) -> DataHealth:
    """Market-wide staleness blocks new entries; per-symbol staleness only excludes the symbol."""
    session = calendar.current_or_next(now)
    market_open = calendar.is_open(now)
    ages = {s: bar_age(latest.get(s), now) for s in subscribed}
    health = DataHealth(market_open=market_open, stale=False, symbol_age_s=ages)
    if not market_open or session is None or now < session.open_at + WARMUP_AFTER_OPEN:
        return health
    health.stale_symbols = sorted(s for s, a in ages.items() if a is None or a > stale_seconds)
    if not stream_connected:
        health.reasons.append("market-data stream disconnected")
    for ref in MARKET_REFERENCE_SYMBOLS:
        age = ages.get(ref)
        if ref not in subscribed:
            health.reasons.append(f"{ref} is not subscribed")
        elif age is None or age > stale_seconds:
            health.reasons.append(f"{ref} has no bar for "
                                  f"{'the session' if age is None else f'{age:.0f}s'}")
    health.stale = bool(health.reasons)
    return health
