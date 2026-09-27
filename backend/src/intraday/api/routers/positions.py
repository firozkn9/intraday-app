"""/positions, /account (docs/design/05 §5). Read-only; closes go through engine commands."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from intraday.api.deps import (
    DB,
    Admin,
    Ctx,
    CurrentPrincipal,
    client_ip,
    mutation_limit,
    read_limit,
    user_agent,
)
from intraday.api.errors import Problem
from intraday.core.runtime_config import parse_config
from intraday.core.timeutil import ET, utcnow
from intraday.db.models import AccountSnapshot, Fill, Order, OrderEvent, Position, PositionEvent
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import portfolio as pf_repo
from intraday.db.repositories import settings as settings_repo
from intraday.db.repositories import trading as trading_repo
from intraday.db.repositories import users as users_repo

router = APIRouter(tags=["positions"], dependencies=[Depends(read_limit)])


def _s(x: Any) -> str | None:
    return None if x is None else str(x)


def _position_json(p: Position, mark: Decimal | None) -> dict[str, Any]:
    avg = Decimal(p.avg_entry_price) if p.avg_entry_price is not None else None
    live = p.status in trading_repo.LIVE_POSITION
    current = mark if live else (Decimal(p.avg_exit_price) if p.avg_exit_price else None)
    unrealized = ((current - avg) * p.qty).quantize(Decimal("0.01")) \
        if live and current is not None and avg is not None and p.qty else None
    pct = float((current - avg) / avg * 100) if current is not None and avg else None
    held = p.exit_time or utcnow()
    return {"id": str(p.id), "symbol": p.symbol, "status": p.status, "qty": p.qty,
            "avg_entry": _s(p.avg_entry_price), "current": _s(current),
            "unrealized_pnl": _s(unrealized), "pnl_pct": round(pct, 3) if pct is not None else None,
            "stop": _s(p.stop_price), "target": _s(p.take_profit_price),
            "initial_stop": _s(p.initial_stop),
            "confidence": float(p.score) if p.score is not None else None,
            "strategy": p.strategy, "sector": p.sector, "trailing_active": p.trailing_active,
            "entry_time": p.entry_time.isoformat() if p.entry_time else None,
            "holding_seconds": int((held - p.entry_time).total_seconds()) if p.entry_time
            else None,
            "exit_time": p.exit_time.isoformat() if p.exit_time else None,
            "exit_reason": p.exit_reason, "avg_exit": _s(p.avg_exit_price),
            "net_pnl": _s(p.net_pnl), "return_pct": float(p.return_pct) * 100
            if p.return_pct is not None else None,
            "session_date": str(p.session_date),
            "entry_decision_id": _s(p.entry_decision_id)}


def _order_json(o: Order) -> dict[str, Any]:
    return {"id": str(o.id), "client_order_id": o.client_order_id,
            "broker_order_id": o.broker_order_id, "parent_order_id": _s(o.parent_order_id),
            "intent": o.intent, "side": o.side, "type": o.type, "order_class": o.order_class,
            "qty": o.qty, "filled_qty": o.filled_qty, "limit_price": _s(o.limit_price),
            "stop_price": _s(o.stop_price), "filled_avg_price": _s(o.filled_avg_price),
            "status": o.status, "broker_status": o.broker_status, "attempts": o.attempts,
            "last_error": o.last_error, "reject_reason": o.reject_reason,
            "created_at": o.created_at.isoformat(),
            "terminal_at": o.terminal_at.isoformat() if o.terminal_at else None}


async def _feed(db: DB) -> str:
    active = await settings_repo.get_active(db)
    return parse_config(active.config).data.feed if active else "iex"


@router.get("/positions")
async def list_positions(principal: CurrentPrincipal, db: DB,
                         scope: Literal["open", "today"] = Query("open")
                         ) -> list[dict[str, Any]]:
    if scope == "open":
        rows = await trading_repo.live_positions(db)
    else:
        today = utcnow().astimezone(ET).date()
        rows = list((await db.execute(
            select(Position).where((Position.session_date == today)
                                   | Position.status.in_(trading_repo.LIVE_POSITION))
            .order_by(Position.created_at.desc()))).scalars())
    marks = await pf_repo.latest_marks(db, await _feed(db), sorted({p.symbol for p in rows}))
    return [_position_json(p, marks.get(p.symbol)) for p in rows]


@router.get("/positions/{position_id}")
async def get_position(position_id: uuid.UUID, principal: CurrentPrincipal, db: DB
                       ) -> dict[str, Any]:
    p = await trading_repo.get_position(db, position_id)
    if p is None:
        raise Problem(404, "Position not found")
    marks = await pf_repo.latest_marks(db, await _feed(db), [p.symbol])
    orders = await trading_repo.position_orders(db, p.id)
    ids = [o.id for o in orders]
    events = list((await db.execute(select(OrderEvent).where(OrderEvent.order_id.in_(ids))
                                    .order_by(OrderEvent.ts))).scalars()) if ids else []
    fills = list((await db.execute(select(Fill).where(Fill.order_id.in_(ids))
                                   .order_by(Fill.ts))).scalars()) if ids else []
    pevents = list((await db.execute(select(PositionEvent).where(
        PositionEvent.position_id == p.id).order_by(PositionEvent.ts))).scalars())
    return {**_position_json(p, marks.get(p.symbol)),
            "exit_detail": p.exit_detail, "risk_amount": _s(p.risk_amount),
            "orders": [_order_json(o) for o in orders],
            "order_events": [{"order_id": str(e.order_id), "ts": e.ts.isoformat(),
                              "source": e.source, "event": e.event, "payload": e.payload}
                             for e in events],
            "fills": [{"order_id": str(f.order_id), "ts": f.ts.isoformat(), "qty": f.qty,
                       "price": str(f.price)} for f in fills],
            "events": [{"ts": e.ts.isoformat(), "type": e.type, "details": e.details}
                       for e in pevents]}


class CloseIn(BaseModel):
    confirm: str


@router.post("/positions/{position_id}/close", status_code=202,
             dependencies=[Depends(mutation_limit)])
async def close_position(position_id: uuid.UUID, body: CloseIn, principal: Admin,
                         request: Request, ctx: Ctx, db: DB) -> dict[str, Any]:
    """Manual close (exit_reason MANUAL). Type the symbol to confirm."""
    p = await trading_repo.get_position(db, position_id)
    if p is None:
        raise Problem(404, "Position not found")
    if p.status not in ("OPEN", "EXITING", "PENDING_ENTRY"):
        raise Problem(409, f"Position is {p.status}")
    if body.confirm.strip().upper() != p.symbol:
        raise Problem(422, "Confirmation required", f'Type "{p.symbol}" to confirm.')
    cmd = await engine_repo.enqueue_command(db, command="CLOSE_POSITION",
                                            user_id=principal.user.id,
                                            params={"position_id": str(p.id)})
    await users_repo.audit(db, action="COMMAND_CLOSE_POSITION", user_id=principal.user.id,
                           target=f"position:{p.id}", after={"symbol": p.symbol},
                           ip=client_ip(request), user_agent=user_agent(request))
    await ctx.publish("commands:new", {"id": str(cmd.id)})
    return {"command_id": str(cmd.id), "status": cmd.status}


@router.get("/account")
async def account(principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    row = (await db.execute(select(AccountSnapshot).order_by(AccountSnapshot.ts.desc())
                            .limit(1))).scalar_one_or_none()
    if row is None:
        return {"as_of": None, "source": None}
    return {"as_of": row.ts.isoformat(), "source": row.source, "equity": str(row.equity),
            "cash": str(row.cash), "buying_power": str(row.buying_power),
            "long_market_value": str(row.long_market_value), "exposure": str(row.exposure),
            "open_positions": row.open_positions,
            "realized_pnl_today": str(row.realized_pnl_today),
            "unrealized_pnl": str(row.unrealized_pnl), "daily_pnl": str(row.daily_pnl)}
