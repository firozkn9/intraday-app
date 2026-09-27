"""Deterministic synthetic market data for replay and end-to-end tests (no network).

``seed_demo`` writes: the watchlist symbol AAA (a tradable U.S. common stock with
cached Alpaca asset data), SPY/QQQ reference bars, 12 prior sessions of history,
and one session in which AAA consolidates, breaks out on rising volume and then
trends to its take-profit. Only for simulation databases (the CLI checks).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.timeutil import ET
from intraday.db.models import Symbol
from intraday.db.repositories import marketdata as md_repo
from intraday.db.session import transaction
from intraday.marketdata.calendar import SessionDay
from intraday.marketdata.types import Bar

SESSION_MINUTES = 390


def _session(d: date) -> SessionDay:
    return SessionDay.from_alpaca({"date": d.isoformat(), "open": "09:30", "close": "16:00"})


def _bars(symbol: str, session: SessionDay, closes: Sequence[float],
          volumes: Sequence[float]) -> list[Bar]:
    out, prev = [], closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes, strict=True)):
        o = prev
        hi, lo = max(o, c) + 0.05, min(o, c) - 0.05

        def d(x: float) -> Decimal:
            return Decimal(str(round(x, 4)))

        out.append(Bar(symbol, "iex", session.open_at + timedelta(minutes=i), d(o), d(hi),
                       d(lo), d(c), int(v), d((hi + lo + c) / 3)))
        prev = c
    return out


def _uptrend(n: int, start: float, step: float) -> list[float]:
    return [round(start + step * i, 4) for i in range(n)]


def demo_day() -> tuple[list[float], list[float]]:
    """AAA: trend, 15-bar base, breakout, run to target, then drift to the close."""
    base = _uptrend(25, 100.0, 0.04)
    flat = [101.0 + (0.05 if i % 2 else -0.05) for i in range(15)]
    breakout = [101.1, 101.25, 101.5]
    run = _uptrend(60, 101.55, 0.05)                      # → ~104.5, past a 2% target
    rest = [104.5 + (0.03 if i % 2 else -0.03) for i in range(SESSION_MINUTES - 103)]
    closes = base + flat + breakout + run + rest
    vols = [1000.0] * 40 + [1300.0, 1800.0, 2600.0] + [1500.0] * 60 + [800.0] * len(rest)
    return closes, vols


def prior_days(day: date, n: int = 12) -> list[date]:
    out: list[date] = []
    d = day - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


async def seed_demo(sm: async_sessionmaker[AsyncSession], day: date) -> int:
    today = _session(day)
    priors = [_session(d) for d in prior_days(day)]
    rows: list[Bar] = []
    for sess in priors:
        for sym, start in (("AAA", 100.0), ("SPY", 570.0), ("QQQ", 480.0)):
            closes = [start + 0.02 * i + (0.03 if i % 2 else -0.03)
                      for i in range(SESSION_MINUTES)]
            rows += _bars(sym, sess, closes, [500.0] * SESSION_MINUTES)
    aaa, vols = demo_day()
    rows += _bars("AAA", today, aaa, vols)
    rows += _bars("SPY", today, _uptrend(SESSION_MINUTES, 570.0, 0.05), [1000.0] * SESSION_MINUTES)
    rows += _bars("QQQ", today, _uptrend(SESSION_MINUTES, 480.0, 0.05), [1000.0] * SESSION_MINUTES)
    synced = datetime.combine(day, datetime.min.time(), ET)
    async with transaction(sm) as s:
        await s.execute(insert(Symbol).values(
            symbol="AAA", role="TRADABLE", asset_type="COMMON_STOCK", country="US",
            sector="Technology", alpaca_class="us_equity", alpaca_exchange="NASDAQ",
            alpaca_name="Aaa Inc. Common Stock", alpaca_tradable=True, alpaca_status="active",
            alpaca_synced_at=synced).on_conflict_do_nothing(index_elements=["symbol"]))
        for i in range(0, len(rows), 5000):
            await md_repo.upsert_bars(s, rows[i:i + 5000])
        await md_repo.upsert_calendar(s, [*priors, today])
    return len(rows)
