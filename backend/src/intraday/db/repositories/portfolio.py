"""Portfolio and account snapshots for the risk engine, from the database.

Positions/orders are written by the order manager (Phase 8); this reads them so
the risk engine is correct from the first trade on.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from intraday.db.models import AccountSnapshot as AccountRow
from intraday.db.models import Bar1m, Order, Position, Symbol
from intraday.risk.models import (
    ZERO,
    AccountSnapshot,
    OpenPosition,
    PendingEntry,
    PortfolioSnapshot,
)

OPEN_STATUSES = ("OPEN", "EXITING")
PENDING_ORDER_STATUSES = ("PENDING_SUBMIT", "SUBMITTED", "UNKNOWN", "PARTIALLY_FILLED",
                          "PENDING_CANCEL")


async def latest_marks(session: AsyncSession, feed: str, symbols: Sequence[str]
                       ) -> dict[str, Decimal]:
    if not symbols:
        return {}
    latest = (select(Bar1m.symbol, func.max(Bar1m.ts).label("ts"))
              .where(Bar1m.feed == feed, Bar1m.symbol.in_(symbols))
              .group_by(Bar1m.symbol).subquery())
    res = await session.execute(
        select(Bar1m.symbol, Bar1m.close).join(
            latest, (Bar1m.symbol == latest.c.symbol) & (Bar1m.ts == latest.c.ts))
        .where(Bar1m.feed == feed))
    return {sym: Decimal(close) for sym, close in res.all()}


async def sectors(session: AsyncSession, symbols: Sequence[str]) -> dict[str, str | None]:
    if not symbols:
        return {}
    res = await session.execute(select(Symbol.symbol, Symbol.sector)
                                .where(Symbol.symbol.in_(symbols)))
    return dict(res.all())


async def load_portfolio(session: AsyncSession, today: date, feed: str) -> PortfolioSnapshot:
    open_rows = list((await session.execute(
        select(Position).where(Position.status.in_(OPEN_STATUSES)))).scalars())
    pending_orders = list((await session.execute(
        select(Order).where(Order.intent == "ENTRY",
                            Order.status.in_(PENDING_ORDER_STATUSES)))).scalars())
    today_rows = list((await session.execute(
        select(Position).where(Position.session_date == today))).scalars())

    syms = sorted({p.symbol for p in open_rows} | {o.symbol for o in pending_orders})
    marks = await latest_marks(session, feed, syms)
    sector = await sectors(session, syms)
    pos_by_id = {p.id: p for p in today_rows + open_rows}

    positions = tuple(
        OpenPosition(p.symbol, p.qty, Decimal(p.avg_entry_price or 0),
                     marks.get(p.symbol, Decimal(p.avg_entry_price or 0)),
                     Decimal(p.stop_price or 0), sector.get(p.symbol))
        for p in open_rows if p.qty > 0)
    pending = []
    for o in pending_orders:
        parent = pos_by_id.get(o.position_id) if o.position_id else None
        stop = Decimal((parent.initial_stop if parent else None) or o.stop_price or 0)
        remaining = o.qty - (o.filled_qty or 0)
        if remaining > 0 and o.limit_price is not None:
            pending.append(PendingEntry(o.symbol, remaining, Decimal(o.limit_price), stop,
                                        sector.get(o.symbol)))

    realized = sum((Decimal(p.net_pnl) for p in today_rows
                    if p.status == "CLOSED" and p.net_pnl is not None), ZERO)
    counts = Counter(p.symbol for p in today_rows)
    exits: dict[str, Any] = {}
    for p in today_rows:
        if p.exit_time is not None and (p.symbol not in exits or p.exit_time > exits[p.symbol]):
            exits[p.symbol] = p.exit_time
    return PortfolioSnapshot(positions=positions, pending=tuple(pending),
                             realized_pnl_today=realized, trades_today=len(today_rows),
                             symbol_trades_today=dict(counts), last_exit_at=exits)


async def load_account(session: AsyncSession, fallback_equity: Decimal) -> AccountSnapshot:
    """Latest broker snapshot; before the broker exists (Phase 7), the configured balance."""
    row = (await session.execute(
        select(AccountRow).where(AccountRow.source == "BROKER")
        .order_by(AccountRow.ts.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        return AccountSnapshot(fallback_equity, fallback_equity, fallback_equity, "DERIVED")
    return AccountSnapshot(Decimal(row.equity), Decimal(row.cash), Decimal(row.buying_power),
                           "BROKER")
