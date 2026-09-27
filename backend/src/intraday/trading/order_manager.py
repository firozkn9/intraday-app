"""OrderManager: idempotent submission and the local order state machine (docs/design/08 §4-7).

Submission protocol: persist the order as PENDING_SUBMIT first, then submit.
On a network error or timeout, look the order up by client_order_id before any
retry; retry only with the *same* client_order_id (the broker rejects
duplicates, so a double submit is impossible). An order that stays unknown is
marked UNKNOWN and ``broker_down_reason`` is set, which blocks new entries
until the reconciler settles it.

Broker trade updates, the 10 s poll and reconciliation all flow through
``_apply``, which adopts the broker's status (never moving a terminal order
back), records each fill once, and updates the position.

Invariants enforced here, independent of the risk engine: entries are
whole-share long bracket orders for an eligible U.S. common stock with stop <
limit < target; the gate is re-checked right before submission; no second
live position per symbol; a sell never exceeds min(local qty, broker qty).
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.ids import uuid7
from intraday.core.logging import get_logger
from intraday.core.timeutil import ET, utcnow
from intraday.db.models import Order, Position, Symbol
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import trading as repo
from intraday.db.session import transaction
from intraday.marketdata.watchlist import eligibility_of
from intraday.trading import position_manager as pm
from intraday.trading.broker.base import (
    BracketEntry,
    Broker,
    BrokerError,
    BrokerOrder,
    MarketExit,
    ProtectiveOco,
    TradeUpdate,
)
from intraday.trading.pricing import CENT, cents, client_order_id

log = get_logger(__name__)

STATUS_MAP = {
    "new": "SUBMITTED", "accepted": "SUBMITTED", "pending_new": "SUBMITTED",
    "accepted_for_bidding": "SUBMITTED", "calculated": "SUBMITTED", "held": "SUBMITTED",
    "stopped": "SUBMITTED", "suspended": "SUBMITTED",
    "partially_filled": "PARTIALLY_FILLED", "filled": "FILLED",
    "pending_cancel": "PENDING_CANCEL", "canceled": "CANCELED", "done_for_day": "EXPIRED",
    "expired": "EXPIRED", "rejected": "REJECTED", "replaced": "REPLACED",
}
TERMINAL = frozenset(repo.TERMINAL)
IN_FLIGHT_SECONDS = 5


def local_status(broker_status: str) -> str | None:
    return STATUS_MAP.get(broker_status)


@dataclass(frozen=True)
class EntryRequest:
    decision_id: uuid.UUID
    session_date: date
    symbol: str
    qty: int
    limit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    strategy: str | None = None
    score: Decimal | None = None
    sector: str | None = None
    config_version: int | None = None


@dataclass(frozen=True)
class SubmitResult:
    status: str                  # SUBMITTED | UNKNOWN | REJECTED | FAILED | BLOCKED
    order_id: uuid.UUID | None = None
    position_id: uuid.UUID | None = None
    detail: str | None = None


def entry_problem(req: EntryRequest) -> str | None:
    if req.qty <= 0:
        return "quantity must be a positive whole number"
    for name, px in (("limit", req.limit_price), ("stop", req.stop_price),
                     ("target", req.take_profit_price)):
        if px <= 0 or px != px.quantize(CENT):
            return f"{name} price {px} must be positive and in whole cents"
    if not req.stop_price < req.limit_price < req.take_profit_price:
        return "bracket requires stop < limit < target"
    return None


class OrderManager:
    def __init__(self, sm: async_sessionmaker[AsyncSession], broker: Broker, *,
                 clock: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 retry_delays: Sequence[float] = (0.5, 1.0, 2.0),
                 cancel_confirm_seconds: float = 5.0, poll_step: float = 0.25,
                 entry_blocks: Callable[[], Sequence[str]] | None = None) -> None:
        self.sm = sm
        self.broker = broker
        self.clock = clock
        self.sleep = sleep
        self.retry_delays = tuple(retry_delays)
        self.cancel_confirm_seconds = cancel_confirm_seconds
        self.poll_step = poll_step
        self.entry_blocks = entry_blocks
        self.broker_down_reason: str | None = None
        self._lock = asyncio.Lock()
        self._position_locks: dict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)

    def today(self) -> date:
        return self.clock().astimezone(ET).date()

    # ------------------------------------------------------------------ entries

    async def submit_entry(self, req: EntryRequest) -> SubmitResult:
        blocks = list(self.entry_blocks()) if self.entry_blocks else []
        if blocks:
            return SubmitResult("BLOCKED", detail=",".join(blocks))
        problem = entry_problem(req)
        if problem:
            return SubmitResult("FAILED", detail=problem)
        now = self.clock()
        async with self._lock, transaction(self.sm) as s:
            row = (await s.execute(select(Symbol).where(Symbol.symbol == req.symbol))
                   ).scalar_one_or_none()
            if row is None or row.role != "TRADABLE" or not eligibility_of(row, now).eligible:
                return SubmitResult("FAILED", detail=f"{req.symbol} is not an eligible "
                                                     "tradable common stock")
            if any(p.symbol == req.symbol for p in await repo.live_positions(s)):
                return SubmitResult("FAILED", detail=f"{req.symbol} already has a live position")
            pos = Position(id=uuid7(), session_date=req.session_date, symbol=req.symbol,
                           status="PENDING_ENTRY", strategy=req.strategy,
                           entry_decision_id=req.decision_id, qty=0,
                           initial_stop=req.stop_price, stop_price=req.stop_price,
                           take_profit_price=req.take_profit_price, score=req.score,
                           sector=req.sector, config_version=req.config_version,
                           risk_amount=cents((req.limit_price - req.stop_price) * req.qty),
                           created_at=now, updated_at=now)
            cid = client_order_id(req.session_date, "ENTRY", req.decision_id, 0)
            order = Order(id=uuid7(), client_order_id=cid, position_id=pos.id,
                          decision_id=req.decision_id, symbol=req.symbol, side="buy",
                          intent="ENTRY", type="limit", order_class="bracket", tif="day",
                          qty=req.qty, limit_price=req.limit_price, stop_price=req.stop_price,
                          status="PENDING_SUBMIT", created_at=now, updated_at=now)
            s.add(pos)
            await s.flush()
            s.add(order)
            await s.flush()
            await repo.order_event(s, order.id, source="LOCAL", event="PENDING_SUBMIT",
                                   payload={"take_profit": str(req.take_profit_price),
                                            "stop_loss": str(req.stop_price)})
        status, detail = await self._submit(order.id, cid, lambda: self.broker.submit_bracket_entry(
            BracketEntry(cid, req.symbol, req.qty, req.limit_price, req.take_profit_price,
                         req.stop_price)))
        if status in ("REJECTED", "FAILED"):
            async with self._lock, transaction(self.sm) as s:
                p = await repo.get_position(s, pos.id, lock=True)
                if p is not None and p.status == "PENDING_ENTRY" and p.qty == 0:
                    await pm.abandon(s, p, f"entry {status.lower()}: {detail}", self.clock())
        log.info("entry_submitted", symbol=req.symbol, qty=req.qty, limit=str(req.limit_price),
                 status=status, detail=detail)
        return SubmitResult(status, order.id, pos.id, detail)

    # ------------------------------------------------------------------ submission protocol

    async def _submit(self, order_id: uuid.UUID, cid: str,
                      call: Callable[[], Awaitable[BrokerOrder]]) -> tuple[str, str | None]:
        last: str | None = None
        for attempt in range(len(self.retry_delays) + 1):
            if attempt:
                await self.sleep(self.retry_delays[attempt - 1])
            try:
                bo = await call()
            except BrokerError as exc:
                last = exc.message
                await self._note_attempt(order_id, attempt + 1, exc.message)
                if exc.retryable or attempt > 0:
                    # Never resubmit blindly: the first attempt may have reached the broker.
                    found = await self._lookup(cid)
                    if found is not None:
                        await self.apply_broker_order(found, "POLL", order_id=order_id)
                        return "SUBMITTED", None
                if not exc.retryable:
                    await self._set_status(order_id, "REJECTED", exc.message)
                    return "REJECTED", exc.message
                continue
            await self.apply_broker_order(bo, "LOCAL", order_id=order_id)
            return "SUBMITTED", None
        await self._set_status(order_id, "UNKNOWN", last)
        self.broker_down_reason = f"order {cid} status unknown after retries: {last}"
        log.error("order_status_unknown", cid=cid, error=last)
        return "UNKNOWN", last

    async def _lookup(self, cid: str) -> BrokerOrder | None:
        try:
            return await self.broker.get_order_by_client_id(cid)
        except BrokerError as exc:
            log.warning("order_lookup_failed", cid=cid, error=exc.message)
            return None

    async def _note_attempt(self, order_id: uuid.UUID, attempts: int, error: str) -> None:
        async with self._lock, transaction(self.sm) as s:
            row = await s.get(Order, order_id, with_for_update=True)
            if row is not None:
                row.attempts, row.last_error, row.updated_at = attempts, error[:500], self.clock()

    async def _set_status(self, order_id: uuid.UUID, status: str, error: str | None) -> None:
        async with self._lock, transaction(self.sm) as s:
            row = await s.get(Order, order_id, with_for_update=True)
            if row is None:
                return
            row.status, row.updated_at = status, self.clock()
            if status == "REJECTED":
                row.reject_reason = (error or "")[:500]
            if status in TERMINAL:
                row.terminal_at = self.clock()
            await repo.order_event(s, row.id, source="LOCAL", event=status,
                                   payload={"error": error})

    # ------------------------------------------------------------------ applying broker state

    async def on_trade_update(self, u: TradeUpdate) -> None:
        await self.apply_broker_order(u.order, "STREAM", event=u.event, fill_price=u.price,
                                      fill_qty=u.qty, execution_id=u.execution_id,
                                      ts=u.timestamp)

    async def apply_broker_order(self, bo: BrokerOrder, source: str, *,
                                 order_id: uuid.UUID | None = None, event: str | None = None,
                                 fill_price: Decimal | None = None, fill_qty: int | None = None,
                                 execution_id: str | None = None, ts: datetime | None = None
                                 ) -> bool:
        async with self._lock, transaction(self.sm) as s:
            row = await s.get(Order, order_id, with_for_update=True) if order_id else None
            row = (row or await repo.order_by_broker_id(s, bo.id)
                   or await repo.order_by_client_id(s, bo.client_order_id)
                   or await self._replacement_row(s, bo))
            if row is None:
                log.info("broker_order_unknown", broker_id=bo.id, cid=bo.client_order_id,
                         symbol=bo.symbol, status=bo.status)
                return False
            await self._apply(s, row, bo, source=source, event=event, fill_price=fill_price,
                              fill_qty=fill_qty, execution_id=execution_id, ts=ts)
            return True

    async def _replacement_row(self, s: AsyncSession, bo: BrokerOrder) -> Order | None:
        """A replaced stop gets a new broker id; clone the old row's links for it."""
        if not bo.replaces:
            return None
        old = await repo.order_by_broker_id(s, bo.replaces)
        if old is None:
            return None
        return await self._new_child(s, old, bo, intent=old.intent,
                                     parent_id=old.parent_order_id)

    async def _new_child(self, s: AsyncSession, like: Order, bo: BrokerOrder, *, intent: str,
                         parent_id: uuid.UUID | None) -> Order:
        now = self.clock()
        cid = bo.client_order_id or f"{like.client_order_id}-{bo.id[-8:]}"
        if await repo.order_by_client_id(s, cid) is not None:
            cid = f"{cid}-{bo.id[-8:]}"
        row = Order(id=uuid7(), client_order_id=cid[:128], broker_order_id=bo.id,
                    parent_order_id=parent_id, position_id=like.position_id,
                    decision_id=like.decision_id, symbol=bo.symbol or like.symbol,
                    side=bo.side or "sell", intent=intent, type=bo.type or "limit",
                    order_class=bo.order_class if bo.order_class in ("simple", "bracket", "oco")
                    else "simple", tif=bo.time_in_force or "day", qty=max(bo.qty, 1),
                    limit_price=bo.limit_price, stop_price=bo.stop_price, status="SUBMITTED",
                    broker_status=bo.status, submitted_at=now, created_at=now, updated_at=now)
        s.add(row)
        await s.flush()
        await repo.order_event(s, row.id, source="LOCAL", event="LINKED",
                               payload={"broker_order_id": bo.id, "replaces": bo.replaces})
        return row

    async def _apply(self, s: AsyncSession, row: Order, bo: BrokerOrder, *, source: str,
                     event: str | None = None, fill_price: Decimal | None = None,
                     fill_qty: int | None = None, execution_id: str | None = None,
                     ts: datetime | None = None) -> None:
        now = self.clock()
        at = ts or now
        if row.broker_order_id is None:
            row.broker_order_id = bo.id
            row.submitted_at = row.submitted_at or now

        # 1. Fills: by increase of filled_qty, so the stream, the poll and reconciliation
        #    can all report the same fill without double counting.
        if bo.filled_qty > row.filled_qty:
            delta = bo.filled_qty - row.filled_qty
            if fill_price is not None and fill_qty == delta:
                price = fill_price
            elif bo.filled_avg_price is not None:
                prev = Decimal(row.filled_avg_price or 0) * row.filled_qty
                price = (bo.filled_avg_price * bo.filled_qty - prev) / delta
            else:
                price = fill_price or Decimal(row.limit_price or row.stop_price or 0)
            price = price.quantize(Decimal("0.0001"))
            exec_id = (execution_id if execution_id and fill_qty == delta
                       else f"{bo.id}:{bo.filled_qty}")
            await repo.insert_fill(s, order_id=row.id, execution_id=exec_id, ts=at, qty=delta,
                                   price=price)
            row.filled_qty = bo.filled_qty
            row.filled_avg_price = bo.filled_avg_price or price
            if row.position_id is not None:
                pos = await repo.get_position(s, row.position_id, lock=True)
                if pos is not None:
                    await pm.apply_fill(s, pos, row, delta, price, at)

        # 2. Status: adopt the broker's, but never move a terminal order back.
        new = local_status(bo.status)
        if new is not None and new != row.status and not (row.status in TERMINAL
                                                          and new not in TERMINAL):
            await repo.order_event(s, row.id, source=source, event=event or bo.status,
                                   payload={"from": row.status, "to": new,
                                            "filled_qty": bo.filled_qty})
            row.status = new
            if new == "SUBMITTED" and row.acked_at is None:
                row.acked_at = now
            if new in TERMINAL:
                row.terminal_at = now
        row.broker_status = bo.status
        if bo.stop_price is not None and row.intent == "STOP_LOSS":
            row.stop_price = bo.stop_price
        row.updated_at = now

        # 3. Legs (bracket take-profit/stop, OCO stop).
        for leg in bo.legs:
            lrow = await repo.order_by_broker_id(s, leg.id)
            if lrow is None:
                intent = "TAKE_PROFIT" if leg.type == "limit" else "STOP_LOSS"
                lrow = await self._new_child(s, row, leg, intent=intent, parent_id=row.id)
            await self._apply(s, lrow, leg, source=source, ts=ts)

        # 4. An entry that ended without any fill abandons its position.
        if (row.intent == "ENTRY" and row.status in TERMINAL and row.filled_qty == 0
                and row.position_id is not None):
            pos = await repo.get_position(s, row.position_id, lock=True)
            if pos is not None and pos.status == "PENDING_ENTRY" and pos.qty == 0:
                await pm.abandon(s, pos, f"entry {row.status.lower()}", now)

    async def poll_open_orders(self, source: str = "POLL") -> int:
        """Fetch every non-terminal local order (via its root) and apply the broker's state."""
        async with self.sm() as s:
            rows = await repo.non_terminal_orders(s)
            parent_ids = {r.parent_order_id for r in rows if r.parent_order_id}
            parents = {p.id: p for p in (await s.execute(
                select(Order).where(Order.id.in_(parent_ids)))).scalars()} if parent_ids else {}
        roots: list[str] = []
        for r in rows:
            parent = parents.get(r.parent_order_id) if r.parent_order_id else None
            root = parent.broker_order_id if parent is not None else r.broker_order_id
            if root and root not in roots:
                roots.append(root)
        for root in roots:
            try:
                bo = await self.broker.get_order(root)
            except BrokerError as exc:
                log.warning("order_poll_failed", error=exc.message)
                raise
            if bo is not None:
                await self.apply_broker_order(bo, source)
        return len(roots)

    async def resolve_pending(self, *, min_age_seconds: float = 30) -> int:
        """Settle PENDING_SUBMIT/UNKNOWN orders by client_order_id. Returns unresolved count."""
        async with self.sm() as s:
            rows = await repo.orders_in_status(s, ("PENDING_SUBMIT", "UNKNOWN"))
        unresolved = 0
        now = self.clock()
        for row in rows:
            if (row.status == "PENDING_SUBMIT"
                    and (now - row.created_at).total_seconds() < min_age_seconds):
                continue
            try:
                bo = await self.broker.get_order_by_client_id(row.client_order_id)
            except BrokerError as exc:
                log.warning("pending_resolve_failed", cid=row.client_order_id, error=exc.message)
                unresolved += 1
                continue
            if bo is not None:
                await self.apply_broker_order(bo, "RECONCILE", order_id=row.id)
                continue
            await self._set_status(row.id, "FAILED", "not found at the broker")
            if row.intent == "ENTRY" and row.position_id is not None:
                async with self._lock, transaction(self.sm) as s:
                    pos = await repo.get_position(s, row.position_id, lock=True)
                    if pos is not None and pos.status == "PENDING_ENTRY" and pos.qty == 0:
                        await pm.abandon(s, pos, "entry never reached the broker", now)
        if unresolved == 0 and self.broker_down_reason and "unknown" in self.broker_down_reason:
            self.broker_down_reason = None
        return unresolved

    # ------------------------------------------------------------------ cancels

    async def _cancel(self, row: Order, why: str) -> bool:
        """Request a cancel; 404/422 mean it is already done (settled by the next apply)."""
        if row.broker_order_id is None:
            return False
        try:
            await self.broker.cancel_order(row.broker_order_id)
        except BrokerError as exc:
            if exc.status not in (404, 422):
                log.warning("cancel_failed", cid=row.client_order_id, error=exc.message)
                return False
        async with self._lock, transaction(self.sm) as s:
            r = await s.get(Order, row.id, with_for_update=True)
            if r is not None and r.status not in TERMINAL and r.status != "PENDING_CANCEL":
                await repo.order_event(s, r.id, source="LOCAL", event="CANCEL_REQUESTED",
                                       payload={"reason": why})
                r.status, r.updated_at = "PENDING_CANCEL", self.clock()
        return True

    async def confirm_terminal(self, broker_ids: Sequence[str]) -> bool:
        """Poll the broker until every order is terminal (or the confirm window passes)."""
        waited = 0.0
        pending = [b for b in broker_ids if b]
        while pending:
            still = []
            for bid in pending:
                try:
                    bo = await self.broker.get_order(bid)
                except BrokerError:
                    still.append(bid)
                    continue
                if bo is None:
                    continue
                await self.apply_broker_order(bo, "POLL")
                if not bo.terminal:
                    still.append(bid)
            pending = still
            if not pending or waited >= self.cancel_confirm_seconds:
                break
            await self.sleep(self.poll_step)
            waited += self.poll_step
        return not pending

    async def cancel_open_entries(self, why: str) -> int:
        async with self.sm() as s:
            rows = [r for r in await repo.non_terminal_orders(s)
                    if r.intent == "ENTRY" and r.status != "PENDING_CANCEL"]
        n = 0
        for row in rows:
            n += await self._cancel(row, why)
        return n

    async def check_entry_timeouts(self, timeout_seconds: int) -> int:
        """Cancel the unfilled remainder of entries older than the timeout (08 §6.4)."""
        cutoff = self.clock() - timedelta(seconds=timeout_seconds)
        async with self.sm() as s:
            rows = [r for r in await repo.orders_in_status(s, ("SUBMITTED", "PARTIALLY_FILLED"))
                    if r.intent == "ENTRY" and (r.submitted_at or r.created_at) <= cutoff]
        n = 0
        for row in rows:
            n += await self._cancel(row, f"entry not filled within {timeout_seconds}s")
        if rows:
            await self.confirm_terminal([r.broker_order_id for r in rows
                                          if r.broker_order_id])
        return n

    # ------------------------------------------------------------------ exits

    async def _broker_qty(self, symbol: str) -> int:
        return next((p.qty for p in await self.broker.get_positions() if p.symbol == symbol), 0)

    async def exit_position(self, position_id: uuid.UUID, reason: str, intent: str = "ENGINE_EXIT"
                            ) -> str:
        """Engine exit while the bracket is live (08 §7): cancel legs, confirm, market sell."""
        async with self._position_locks[position_id]:
            return await self._exit(position_id, reason, intent)

    async def _exit(self, position_id: uuid.UUID, reason: str, intent: str) -> str:
        async with self._lock, transaction(self.sm) as s:
            pos = await repo.get_position(s, position_id, lock=True)
            if pos is None or pos.status not in ("OPEN", "EXITING") or pos.qty <= 0:
                return "NOT_OPEN"
            orders = await repo.position_orders(s, position_id)
            if any(o.intent in repo.EXIT_INTENTS and o.status not in TERMINAL for o in orders):
                return "ALREADY_EXITING"
            pos.status, pos.updated_at = "EXITING", self.clock()
            pos.exit_detail = {**pm.ledger(pos), "pending_reason": reason}
            await repo.position_event(s, pos.id, "EXIT_SIGNAL", {"reason": reason,
                                                                 "intent": intent})
            protective = [o for o in orders if o.intent in repo.PROTECTIVE_INTENTS
                          and o.status not in TERMINAL]
            entry_live = [o for o in orders if o.intent == "ENTRY" and o.status not in TERMINAL]
            symbol, local_qty = pos.symbol, pos.qty

        for o in entry_live + protective:
            await self._cancel(o, f"exit: {reason}")
        confirmed = await self.confirm_terminal(
            [o.broker_order_id for o in entry_live + protective if o.broker_order_id])
        async with self.sm() as s:
            pos = await repo.get_position(s, position_id)
        if pos is None or pos.status == "CLOSED" or pos.qty <= 0:
            return "CLOSED_BY_LEG"
        if not confirmed:
            await self._reopen(position_id, "cancel of protective legs not confirmed")
            await self._alert("WARN", "EXIT_DEFERRED",
                              f"{symbol}: protective orders did not confirm cancel; "
                              "exit retried next cycle", f"exit-deferred:{position_id}")
            return "CANCEL_UNCONFIRMED"
        try:
            broker_qty = await self._broker_qty(symbol)
        except BrokerError as exc:
            await self._reopen(position_id, f"broker positions unavailable: {exc.message}")
            await self.ensure_protection(position_id)
            return "BROKER_ERROR"
        qty = min(pos.qty, broker_qty)
        if qty <= 0:
            log.error("exit_broker_flat", symbol=symbol, local=local_qty, broker=broker_qty)
            return "BROKER_FLAT"   # the reconciler explains or latches this
        status = await self._submit_exit(pos, qty, intent, reason)
        if status != "SUBMITTED":
            await self._alert("HIGH", "EXIT_FAILED", f"{symbol}: exit order {status.lower()}; "
                              "protective stop re-placed", f"exit-failed:{position_id}")
            await self._reopen(position_id, f"exit order {status.lower()}")
            await self.ensure_protection(position_id)
        return status

    async def _submit_exit(self, pos: Position, qty: int, intent: str, reason: str) -> str:
        return await self.submit_sell(pos.symbol, qty, intent, reason, pos.id)

    async def submit_sell(self, symbol: str, qty: int, intent: str, reason: str,
                          position_id: uuid.UUID | None) -> str:
        """Market sell (exit or liquidation). Callers cap qty at what the broker holds."""
        now = self.clock()
        async with self._lock, transaction(self.sm) as s:
            ref = position_id or uuid7()
            prefix = client_order_id(self.today(), intent, ref, 0)[:-1]
            cid = f"{prefix}{await repo.count_orders_with_prefix(s, prefix)}"
            row = Order(id=uuid7(), client_order_id=cid, position_id=position_id, symbol=symbol,
                        side="sell", intent=intent, type="market", order_class="simple",
                        tif="day", qty=qty, status="PENDING_SUBMIT", created_at=now,
                        updated_at=now)
            s.add(row)
            await s.flush()
            await repo.order_event(s, row.id, source="LOCAL", event="PENDING_SUBMIT",
                                   payload={"reason": reason})
            if position_id is not None:
                await repo.position_event(s, position_id, "EXIT_SUBMITTED",
                                          {"reason": reason, "qty": qty, "cid": cid})
        status, _ = await self._submit(row.id, cid, lambda: self.broker.submit_market_exit(
            MarketExit(cid, symbol, qty)))
        return status

    async def mark_exiting(self, position_ids: Sequence[uuid.UUID], reason: str) -> None:
        """Liquidation: record why the positions are closing before their legs are cancelled."""
        async with self._lock, transaction(self.sm) as s:
            for pid in position_ids:
                pos = await repo.get_position(s, pid, lock=True)
                if pos is None or pos.status not in ("OPEN", "EXITING", "PENDING_ENTRY"):
                    continue
                if pos.status == "OPEN":
                    pos.status = "EXITING"
                pos.exit_detail = {**pm.ledger(pos), "pending_reason": reason}
                pos.updated_at = self.clock()
                await repo.position_event(s, pid, "EXIT_SIGNAL", {"reason": reason})

    async def _reopen(self, position_id: uuid.UUID, why: str) -> None:
        async with self._lock, transaction(self.sm) as s:
            pos = await repo.get_position(s, position_id, lock=True)
            if pos is not None and pos.status == "EXITING":
                pos.status, pos.updated_at = "OPEN", self.clock()
                d = pm.ledger(pos)
                d.pop("pending_reason", None)
                d["exit_retries"] = int(d.get("exit_retries", 0)) + 1
                d["last_exit_problem"] = why
                pos.exit_detail = d

    async def check_exit_timeouts(self, timeout_seconds: int) -> int:
        """An exit not filled in time: alert HIGH, cancel it, re-protect, retry later."""
        cutoff = self.clock() - timedelta(seconds=timeout_seconds)
        async with self.sm() as s:
            rows = [r for r in await repo.orders_in_status(s, ("SUBMITTED", "PARTIALLY_FILLED"))
                    if r.intent in repo.EXIT_INTENTS and (r.submitted_at or r.created_at) <= cutoff]
        for row in rows:
            await self._alert("HIGH", "EXIT_NOT_FILLED",
                              f"{row.symbol}: exit order not filled within {timeout_seconds}s; "
                              "cancelling, re-protecting and retrying", f"exit-stuck:{row.id}")
            await self._cancel(row, "exit fill timeout")
            await self.confirm_terminal([row.broker_order_id] if row.broker_order_id else [])
            if row.position_id is not None:
                await self._reopen(row.position_id, "exit fill timeout")
                await self.ensure_protection(row.position_id)
        return len(rows)

    # ------------------------------------------------------------------ protection

    async def ensure_protection(self, position_id: uuid.UUID) -> str:
        """Every OPEN position has a live broker stop for its full quantity (08 §6.4, §9)."""
        async with self.sm() as s:
            pos = await repo.get_position(s, position_id)
            orders = await repo.position_orders(s, position_id) if pos else []
        if pos is None or pos.status != "OPEN" or pos.qty <= 0:
            return "NOT_APPLICABLE"
        if any(o.intent == "ENTRY" and o.status not in TERMINAL for o in orders):
            return "ENTRY_IN_PROGRESS"      # bracket legs activate when the entry completes
        live = [o for o in orders if o.intent in repo.PROTECTIVE_INTENTS
                and o.status not in TERMINAL and o.status != "PENDING_SUBMIT"]
        stops = [o for o in live if o.intent == "STOP_LOSS" and o.broker_status != "held"
                 and o.status != "PENDING_CANCEL"]
        if stops and all(o.qty - o.filled_qty == pos.qty for o in stops):
            return "OK"
        if pos.stop_price is None or pos.take_profit_price is None:
            await self._alert("HIGH", "UNPROTECTED_POSITION",
                              f"{pos.symbol}: no stop price known; close it manually",
                              f"unprotected:{position_id}")
            return "NO_PRICES"
        for o in live:
            await self._cancel(o, "re-protect with correct quantity")
        if live and not await self.confirm_terminal(
                [o.broker_order_id for o in live if o.broker_order_id]):
            return "CANCEL_UNCONFIRMED"
        try:
            qty = min(pos.qty, await self._broker_qty(pos.symbol))
        except BrokerError:
            return "BROKER_ERROR"
        if qty <= 0:
            return "BROKER_FLAT"
        now = self.clock()
        stop, target = Decimal(pos.stop_price), Decimal(pos.take_profit_price)
        async with self._lock, transaction(self.sm) as s:
            prefix = client_order_id(self.today(), "PROTECT", pos.id, 0)[:-1]
            cid = f"{prefix}{await repo.count_orders_with_prefix(s, prefix)}"
            row = Order(id=uuid7(), client_order_id=cid, position_id=pos.id, symbol=pos.symbol,
                        side="sell", intent="TAKE_PROFIT", type="limit", order_class="oco",
                        tif="day", qty=qty, limit_price=target, stop_price=stop,
                        status="PENDING_SUBMIT", created_at=now, updated_at=now)
            s.add(row)
            await s.flush()
            await repo.order_event(s, row.id, source="LOCAL", event="PENDING_SUBMIT",
                                   payload={"protect": True})
        status, detail = await self._submit(row.id, cid, lambda: self.broker.submit_protective_oco(
            ProtectiveOco(cid, pos.symbol, qty, target, stop)))
        await self._alert("WARN" if status == "SUBMITTED" else "HIGH", "PROTECTION_REPLACED",
                          f"{pos.symbol}: protective stop re-placed at ${stop} "
                          f"(qty {qty}): {status.lower()}"
                          + (f" - {detail}" if detail else ""), f"protect:{position_id}")
        return status

    async def raise_stop(self, position_id: uuid.UUID, new_stop: Decimal) -> bool:
        """Trailing stop: replace the live stop leg; stops are only ever raised."""
        new_stop = cents(new_stop)
        async with self.sm() as s:
            pos = await repo.get_position(s, position_id)
            orders = await repo.position_orders(s, position_id) if pos else []
        if pos is None or pos.status != "OPEN":
            return False
        stop = next((o for o in orders if o.intent == "STOP_LOSS" and o.broker_order_id
                     and o.status in ("SUBMITTED", "PARTIALLY_FILLED")
                     and o.broker_status != "held"), None)
        current = Decimal(stop.stop_price or 0) if stop else Decimal(pos.stop_price or 0)
        if stop is None or stop.broker_order_id is None or new_stop <= current:
            return False
        try:
            bo = await self.broker.replace_stop(stop.broker_order_id, new_stop)
        except BrokerError as exc:
            log.warning("stop_replace_failed", symbol=pos.symbol, error=exc.message)
            return False
        async with self._lock, transaction(self.sm) as s:
            old = await s.get(Order, stop.id, with_for_update=True)
            if old is not None and old.status not in TERMINAL:
                await repo.order_event(s, old.id, source="LOCAL", event="replaced",
                                       payload={"from": old.status, "to": "REPLACED",
                                                "new_stop": str(new_stop)})
                old.status, old.terminal_at = "REPLACED", self.clock()
            if old is not None and await repo.order_by_broker_id(s, bo.id) is None:
                await self._new_child(s, old, bo, intent="STOP_LOSS",
                                      parent_id=old.parent_order_id)
            p = await repo.get_position(s, position_id, lock=True)
            if p is not None:
                p.stop_price, p.updated_at = new_stop, self.clock()
                await repo.position_event(s, p.id, "STOP_MOVED",
                                          {"from": str(current), "to": str(new_stop)})
        return True

    # ------------------------------------------------------------------ alerts

    async def _alert(self, severity: str, type_: str, message: str, dedupe: str) -> None:
        log.warning("trading_alert", severity=severity, type=type_, message=message)
        async with transaction(self.sm) as s:
            await engine_repo.raise_alert(s, severity=severity, type=type_, message=message,
                                          dedupe_key=dedupe)

    def snapshot(self) -> dict[str, Any]:
        return {"broker_down_reason": self.broker_down_reason}
