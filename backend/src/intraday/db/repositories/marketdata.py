"""Bars, daily bars, calendar, and watchlist symbol persistence."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from intraday.db.models import Bar1d, Bar1m, MarketCalendarDay, Symbol, UniverseScan
from intraday.marketdata.calendar import MarketCalendar, SessionDay
from intraday.marketdata.types import Bar, DailyBar

# asyncpg allows 32767 bind parameters per statement.
_CHUNK = 2000


async def upsert_bars(session: AsyncSession, bars: Sequence[Bar]) -> int:
    # One statement can't touch the same row twice: keep the last bar per key.
    bars = list({(b.symbol, b.feed, b.ts): b for b in bars}.values())
    if not bars:
        return 0
    for i in range(0, len(bars), _CHUNK):
        rows = [b.to_row() for b in bars[i:i + _CHUNK]]
        stmt = insert(Bar1m).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "feed", "ts"],
            set_={c: stmt.excluded[c] for c in ("open", "high", "low", "close", "volume",
                                                "vwap", "trade_count", "is_updated")})
        await session.execute(stmt)
    return len(bars)


async def upsert_daily_bars(session: AsyncSession, bars: Sequence[DailyBar]) -> int:
    bars = list({(b.symbol, b.feed, b.session_date): b for b in bars}.values())
    if not bars:
        return 0
    for i in range(0, len(bars), _CHUNK):
        stmt = insert(Bar1d).values([b.to_row() for b in bars[i:i + _CHUNK]])
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "feed", "session_date"],
            set_={c: stmt.excluded[c] for c in ("open", "high", "low", "close", "volume", "vwap")})
        await session.execute(stmt)
    return len(bars)


async def latest_bar_ts(session: AsyncSession, feed: str, symbols: Sequence[str]
                        ) -> dict[str, datetime]:
    if not symbols:
        return {}
    res = await session.execute(
        select(Bar1m.symbol, func.max(Bar1m.ts)).where(Bar1m.feed == feed,
                                                       Bar1m.symbol.in_(symbols))
        .group_by(Bar1m.symbol))
    return {sym: ts for sym, ts in res.all()}


async def get_bars(session: AsyncSession, symbol: str, feed: str, start: datetime,
                   end: datetime, limit: int = 5000) -> list[Bar1m]:
    res = await session.execute(
        select(Bar1m).where(Bar1m.symbol == symbol, Bar1m.feed == feed, Bar1m.ts >= start,
                            Bar1m.ts < end).order_by(Bar1m.ts).limit(limit))
    return list(res.scalars())


async def get_daily_bars(session: AsyncSession, symbol: str, feed: str, start: date,
                         end: date) -> list[Bar1d]:
    res = await session.execute(
        select(Bar1d).where(Bar1d.symbol == symbol, Bar1d.feed == feed,
                            Bar1d.session_date >= start, Bar1d.session_date <= end)
        .order_by(Bar1d.session_date))
    return list(res.scalars())


async def upsert_calendar(session: AsyncSession, days: Iterable[SessionDay]) -> int:
    rows = [d.to_row() for d in days]
    if not rows:
        return 0
    stmt = insert(MarketCalendarDay).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["session_date"],
        set_={"open_at": stmt.excluded.open_at, "close_at": stmt.excluded.close_at,
              "early_close": stmt.excluded.early_close, "fetched_at": func.now()})
    await session.execute(stmt)
    return len(rows)


async def load_calendar(session: AsyncSession, start: date, end: date) -> MarketCalendar:
    res = await session.execute(
        select(MarketCalendarDay).where(MarketCalendarDay.session_date >= start,
                                        MarketCalendarDay.session_date <= end))
    return MarketCalendar(SessionDay(r.session_date, r.open_at, r.close_at) for r in res.scalars())


async def list_symbols(session: AsyncSession, *, enabled_only: bool = False) -> list[Symbol]:
    stmt = select(Symbol).order_by(Symbol.role.desc(), Symbol.symbol)
    if enabled_only:
        stmt = stmt.where(Symbol.enabled)
    return list((await session.execute(stmt)).scalars())


async def seed_symbols(session: AsyncSession, rows: Sequence[dict[str, Any]], *,
                       only_if_empty: bool) -> int:
    """Insert seed rows that don't exist yet. Never overwrites existing entries."""
    if only_if_empty:
        count = (await session.execute(select(func.count()).select_from(Symbol))).scalar_one()
        if count:
            return 0
    if not rows:
        return 0
    stmt = insert(Symbol).values(list(rows)).on_conflict_do_nothing(index_elements=["symbol"])
    result = await session.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def update_symbol(session: AsyncSession, symbol: str, **values: Any) -> None:
    await session.execute(update(Symbol).where(Symbol.symbol == symbol)
                          .values(**values, updated_at=func.now()))


# ------------------------------------------------------------------ universe scans

async def daily_bars_between(session: AsyncSession, feed: str, symbols: Sequence[str],
                             start: date, end_exclusive: date) -> list[Bar1d]:
    if not symbols:
        return []
    res = await session.execute(
        select(Bar1d).where(Bar1d.feed == feed, Bar1d.symbol.in_(symbols),
                            Bar1d.session_date >= start, Bar1d.session_date < end_exclusive))
    return list(res.scalars())


async def bars_since(session: AsyncSession, feed: str, symbols: Sequence[str],
                     since: datetime) -> list[Bar1m]:
    if not symbols:
        return []
    res = await session.execute(
        select(Bar1m).where(Bar1m.feed == feed, Bar1m.symbol.in_(symbols), Bar1m.ts >= since)
        .order_by(Bar1m.symbol, Bar1m.ts))
    return list(res.scalars())


async def insert_scan(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    if rows:
        await session.execute(insert(UniverseScan).values(list(rows)))


async def latest_scan(session: AsyncSession) -> list[UniverseScan]:
    newest = (await session.execute(
        select(UniverseScan.scan_id).order_by(UniverseScan.ts.desc()).limit(1))).scalar()
    if newest is None:
        return []
    res = await session.execute(select(UniverseScan).where(UniverseScan.scan_id == newest)
                                .order_by(UniverseScan.passed.desc(), UniverseScan.symbol))
    return list(res.scalars())


async def scan_history(session: AsyncSession, symbol: str, limit: int) -> list[UniverseScan]:
    res = await session.execute(select(UniverseScan).where(UniverseScan.symbol == symbol)
                                .order_by(UniverseScan.ts.desc()).limit(limit))
    return list(res.scalars())


def new_scan_id() -> uuid.UUID:
    from intraday.core.ids import uuid7
    return uuid7()
