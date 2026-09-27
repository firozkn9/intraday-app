"""Orders, fills, positions and daily P&L (written by the engine's order manager)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from intraday.db.models import (
    DailyPnl,
    Fill,
    Order,
    OrderEvent,
    Position,
    PositionEvent,
    StrategyPerformance,
)

TERMINAL = ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED", "FAILED")
LIVE_POSITION = ("PENDING_ENTRY", "OPEN", "EXITING")
PROTECTIVE_INTENTS = ("TAKE_PROFIT", "STOP_LOSS")
EXIT_INTENTS = ("ENGINE_EXIT", "FORCE_CLOSE", "EMERGENCY_CLOSE", "MANUAL_CLOSE")


async def order_by_broker_id(s: AsyncSession, broker_id: str) -> Order | None:
    return (await s.execute(select(Order).where(Order.broker_order_id == broker_id)
                            .with_for_update())).scalar_one_or_none()


async def order_by_client_id(s: AsyncSession, cid: str) -> Order | None:
    return (await s.execute(select(Order).where(Order.client_order_id == cid)
                            .with_for_update())).scalar_one_or_none()


async def count_orders_with_prefix(s: AsyncSession, prefix: str) -> int:
    return int((await s.execute(select(func.count()).select_from(Order)
                                .where(Order.client_order_id.like(f"{prefix}%")))).scalar_one())


async def non_terminal_orders(s: AsyncSession) -> list[Order]:
    return list((await s.execute(select(Order).where(Order.status.not_in(TERMINAL))
                                 .order_by(Order.created_at))).scalars())


async def orders_in_status(s: AsyncSession, statuses: Sequence[str]) -> list[Order]:
    return list((await s.execute(select(Order).where(Order.status.in_(statuses))
                                 .order_by(Order.created_at))).scalars())


async def position_orders(s: AsyncSession, position_id: uuid.UUID) -> list[Order]:
    return list((await s.execute(select(Order).where(Order.position_id == position_id)
                                 .order_by(Order.created_at))).scalars())


async def live_positions(s: AsyncSession) -> list[Position]:
    return list((await s.execute(select(Position).where(Position.status.in_(LIVE_POSITION))
                                 .order_by(Position.created_at))).scalars())


async def get_position(s: AsyncSession, position_id: uuid.UUID, *, lock: bool = False
                       ) -> Position | None:
    stmt = select(Position).where(Position.id == position_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await s.execute(stmt)).scalar_one_or_none()


async def order_event(s: AsyncSession, order_id: uuid.UUID, *, source: str, event: str,
                      payload: dict[str, Any]) -> None:
    s.add(OrderEvent(order_id=order_id, source=source, event=event, payload=payload))


async def position_event(s: AsyncSession, position_id: uuid.UUID, type: str,
                         details: dict[str, Any]) -> None:
    s.add(PositionEvent(position_id=position_id, type=type, details=details))


async def insert_fill(s: AsyncSession, *, order_id: uuid.UUID, execution_id: str, ts: datetime,
                      qty: int, price: Decimal) -> bool:
    """Insert a fill once per execution id. Returns False for a duplicate."""
    res = await s.execute(insert(Fill).values(
        order_id=order_id, broker_execution_id=execution_id, ts=ts, qty=qty, price=price)
        .on_conflict_do_nothing(index_elements=["broker_execution_id"]).returning(Fill.id))
    return res.scalar_one_or_none() is not None


async def refresh_daily_pnl(s: AsyncSession, day: date) -> None:
    """Recompute the session's realized P&L from its closed positions (idempotent)."""
    closed = list((await s.execute(select(Position).where(
        Position.session_date == day, Position.status == "CLOSED"))).scalars())
    pnls = [Decimal(p.net_pnl) for p in closed if p.net_pnl is not None]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gross = sum((Decimal(p.gross_pnl or 0) for p in closed), Decimal(0))
    fees = sum((Decimal(p.fees or 0) for p in closed), Decimal(0))
    shares = sum(int((p.exit_detail or {}).get("exit_qty", 0)) for p in closed)
    notional = sum((Decimal(str((p.exit_detail or {}).get("entry_notional", 0)))
                    + Decimal(str((p.exit_detail or {}).get("exit_notional", 0)))
                    for p in closed), Decimal(0))
    values = {
        "realized_gross": gross, "fees": fees, "realized_net": sum(pnls, Decimal(0)),
        "trades": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": (Decimal(len(wins)) / len(pnls)).quantize(Decimal("0.0001"))
        if pnls else None,
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
        "shares_traded": shares * 2, "notional_traded": notional.quantize(Decimal("0.01")),
    }
    await s.execute(insert(DailyPnl).values(session_date=day, **values)
                    .on_conflict_do_update(index_elements=["session_date"], set_=values))
    # strategy_performance for the same session
    from intraday.analytics.performance import strategy_stats, trade_of

    for strategy, row in strategy_stats([trade_of(p) for p in closed]).items():
        await s.execute(insert(StrategyPerformance).values(
            session_date=day, strategy=strategy, **row).on_conflict_do_update(
            index_elements=["session_date", "strategy"], set_=row))


async def set_daily_pnl(s: AsyncSession, day: date, **values: Any) -> None:
    await s.execute(insert(DailyPnl).values(session_date=day, **values)
                    .on_conflict_do_update(index_elements=["session_date"], set_=values))
