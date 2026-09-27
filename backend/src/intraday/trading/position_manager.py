"""Local position state driven by fills (docs/design/08 §1, §6).

PENDING_ENTRY → OPEN (first entry fill) → EXITING (engine exit in progress) →
CLOSED (qty reaches 0), or ABANDONED (entry never filled). Fill quantities and
notionals are kept in ``exit_detail`` so P&L is exact across partial fills.
Called by the order manager inside its transaction.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from intraday.core.logging import get_logger
from intraday.db.models import Order, Position
from intraday.db.repositories import trading as repo

log = get_logger(__name__)

PX = Decimal("0.0001")
CENT = Decimal("0.01")
REASON_BY_INTENT = {"TAKE_PROFIT": "TAKE_PROFIT", "STOP_LOSS": "STOP_LOSS",
                    "ENGINE_EXIT": "TIME_EXIT", "FORCE_CLOSE": "EOD_LIQUIDATION",
                    "EMERGENCY_CLOSE": "EMERGENCY", "MANUAL_CLOSE": "MANUAL"}


def ledger(pos: Position) -> dict[str, Any]:
    d = dict(pos.exit_detail or {})
    for k in ("entry_qty", "exit_qty"):
        d.setdefault(k, 0)
    for k in ("entry_notional", "exit_notional"):
        d.setdefault(k, "0")
    return d


def exit_reason(pos: Position, order: Order) -> str:
    d = pos.exit_detail or {}
    if order.intent in repo.EXIT_INTENTS and d.get("pending_reason"):
        return str(d["pending_reason"])
    if order.intent == "STOP_LOSS" and pos.trailing_active:
        return "TRAILING_STOP"
    return REASON_BY_INTENT.get(order.intent, "RECONCILE")


async def apply_fill(s: AsyncSession, pos: Position, order: Order, qty: int, price: Decimal,
                     ts: datetime) -> str | None:
    """Apply one fill to its position. Returns OPENED, INCREASED, REDUCED, CLOSED or None."""
    d = ledger(pos)
    if order.side == "buy":
        d["entry_qty"] += qty
        d["entry_notional"] = str(Decimal(d["entry_notional"]) + price * qty)
        pos.qty += qty
        pos.avg_entry_price = (Decimal(d["entry_notional"]) / d["entry_qty"]).quantize(PX)
        pos.high_water_price = max(Decimal(pos.high_water_price or price), price)
        if pos.initial_stop is not None:
            pos.risk_amount = ((pos.avg_entry_price - Decimal(pos.initial_stop))
                               * pos.qty).quantize(CENT)
        pos.exit_detail = d
        if pos.status == "PENDING_ENTRY":
            pos.status, pos.entry_time = "OPEN", ts
            return "OPENED"
        return "INCREASED"

    sold = min(qty, pos.qty)
    if sold < qty:
        log.error("sell_fill_exceeds_position", position=str(pos.id), qty=qty, held=pos.qty)
    if sold <= 0:
        return None
    d["exit_qty"] += sold
    d["exit_notional"] = str(Decimal(d["exit_notional"]) + price * sold)
    pos.qty -= sold
    pos.avg_exit_price = (Decimal(d["exit_notional"]) / d["exit_qty"]).quantize(PX)
    pos.exit_detail = d
    if pos.qty > 0:
        await repo.position_event(s, pos.id, "PARTIAL_EXIT",
                                  {"qty": sold, "price": str(price), "remaining": pos.qty})
        return "REDUCED"
    await close(s, pos, exit_reason(pos, order), ts)
    return "CLOSED"


async def close(s: AsyncSession, pos: Position, reason: str, ts: datetime) -> None:
    d = ledger(pos)
    exit_qty = int(d["exit_qty"])
    entry_avg = (Decimal(d["entry_notional"]) / d["entry_qty"]) if d["entry_qty"] else Decimal(0)
    cost = entry_avg * exit_qty
    gross = (Decimal(d["exit_notional"]) - cost).quantize(CENT)
    pos.status, pos.exit_reason, pos.exit_time = "CLOSED", reason, ts
    pos.gross_pnl, pos.fees, pos.net_pnl = gross, Decimal("0.00"), gross
    pos.return_pct = (gross / cost).quantize(Decimal("0.000001")) if cost else None
    if pos.entry_time is not None:
        pos.holding_seconds = int((ts - pos.entry_time).total_seconds())
    d.pop("pending_reason", None)
    pos.exit_detail = d
    pos.updated_at = ts
    await repo.refresh_daily_pnl(s, pos.session_date)
    log.info("position_closed", symbol=pos.symbol, reason=reason, pnl=str(gross))


async def abandon(s: AsyncSession, pos: Position, why: str, ts: datetime) -> None:
    """An entry that never filled: keep the row (and its decision) for audit."""
    pos.status, pos.exit_time, pos.updated_at = "ABANDONED", ts, ts
    pos.exit_detail = {**ledger(pos), "abandoned": why}
