"""Reconciler: local state vs broker truth (SPEC §17, §27; docs/design/08 §9).

Runs at boot (the gate stays RECONCILING until a clean pass), every
``execution.reconcile_interval_seconds``, after broker reconnects and on demand.

1. Settle PENDING_SUBMIT/UNKNOWN orders by client_order_id.
2. Refresh every non-terminal local order from the broker (statuses, fills).
3. Compare positions per symbol (skipping symbols with orders in flight < 5 s):
   - local open, broker flat, a closing sell found at the broker → close locally
     with that fill (self-explained);
   - local open, broker flat, nothing explains it → mismatch;
   - quantities differ → broker is truth: local qty is corrected, mismatch;
   - broker position unknown locally → mismatch (UNEXPECTED_POSITION). It is
     never adopted automatically; force-close and the UI's Close include it.
4. Re-place a protective stop for any OPEN position without one.
A mismatch latches RECONCILE_MISMATCH in the engine (manual reset).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.logging import get_logger
from intraday.core.timeutil import utcnow
from intraday.db.repositories import trading as repo
from intraday.db.session import transaction
from intraday.trading import position_manager as pm
from intraday.trading.broker.base import Broker, BrokerError, BrokerOrder
from intraday.trading.order_manager import IN_FLIGHT_SECONDS, OrderManager

log = get_logger(__name__)


@dataclass
class ReconcileReport:
    ts: datetime
    ok: bool = True                         # the pass completed (broker reachable)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    unresolved_orders: int = 0
    error: str | None = None

    @property
    def clean(self) -> bool:
        return self.ok and not self.mismatches and self.unresolved_orders == 0

    def to_json(self) -> dict[str, Any]:
        return {"ts": self.ts.isoformat(), "ok": self.ok, "clean": self.clean,
                "mismatches": self.mismatches, "actions": self.actions,
                "unresolved_orders": self.unresolved_orders, "error": self.error}


class Reconciler:
    def __init__(self, sm: async_sessionmaker[AsyncSession], broker: Broker, om: OrderManager,
                 *, clock: Callable[[], datetime] = utcnow) -> None:
        self.sm = sm
        self.broker = broker
        self.om = om
        self.clock = clock
        self.last: ReconcileReport | None = None
        self.last_ok_at: datetime | None = None

    async def run(self, *, boot: bool = False) -> ReconcileReport:
        now = self.clock()
        report = ReconcileReport(now)
        try:
            report.unresolved_orders = await self.om.resolve_pending(
                min_age_seconds=0 if boot else 30)
            await self.om.poll_open_orders("RECONCILE")
            broker_positions = {p.symbol: p for p in await self.broker.get_positions()}
            since = (self.last_ok_at or now - timedelta(days=1)) - timedelta(minutes=5)
            recent = await self.broker.get_orders_since(since)
        except BrokerError as exc:
            report.ok, report.error = False, exc.message
            self.last = report
            log.warning("reconcile_broker_unavailable", error=exc.message)
            return report

        async with self.sm() as s:
            live = await repo.live_positions(s)
            in_flight = {o.symbol for o in await repo.non_terminal_orders(s)
                         if (now - o.created_at).total_seconds() < IN_FLIGHT_SECONDS}
        local: dict[str, list[Any]] = defaultdict(list)
        for p in live:
            if p.qty > 0 or p.status in ("OPEN", "EXITING"):
                local[p.symbol].append(p)

        for symbol in sorted(set(local) | set(broker_positions)):
            if symbol in in_flight:
                continue
            lqty = sum(p.qty for p in local.get(symbol, []))
            bpos = broker_positions.get(symbol)
            bqty = bpos.qty if bpos else 0
            if lqty == bqty:
                continue
            if bqty < 0:
                report.mismatches.append({"symbol": symbol, "type": "SHORT_POSITION",
                                          "local_qty": lqty, "broker_qty": bqty})
                continue
            if lqty > 0 and bqty == 0:
                if await self._explain_flat(symbol, local[symbol], recent):
                    report.actions.append(f"{symbol}: closed locally from broker fill")
                    continue
                report.mismatches.append({"symbol": symbol, "type": "BROKER_FLAT",
                                          "local_qty": lqty, "broker_qty": 0})
            elif lqty == 0:
                report.mismatches.append({"symbol": symbol, "type": "UNEXPECTED_POSITION",
                                          "local_qty": 0, "broker_qty": bqty})
            else:
                await self._adopt_broker_qty(local[symbol], bqty)
                report.mismatches.append({"symbol": symbol, "type": "QTY_MISMATCH",
                                          "local_qty": lqty, "broker_qty": bqty})
                report.actions.append(f"{symbol}: local qty set to broker qty {bqty}")

        # Protection: every OPEN position keeps a live stop at the broker.
        async with self.sm() as s:
            open_ids = [p.id for p in await repo.live_positions(s) if p.status == "OPEN"
                        and p.symbol not in in_flight]
        for pid in open_ids:
            result = await self.om.ensure_protection(pid)
            if result not in ("OK", "ENTRY_IN_PROGRESS", "NOT_APPLICABLE"):
                report.actions.append(f"protection {pid}: {result}")

        self.last, self.last_ok_at = report, now
        if not report.clean or report.actions:
            log.warning("reconcile_result", **report.to_json())
        return report

    async def _explain_flat(self, symbol: str, positions: list[Any],
                            recent: list[BrokerOrder]) -> bool:
        """A filled sell at the broker that we have not applied closes the position."""
        need = sum(p.qty for p in positions)
        sells = [o for o in _flatten(recent) if o.symbol == symbol and o.side == "sell"
                 and o.filled_qty > 0]
        for o in sells:                         # apply any we know about but missed
            await self.om.apply_broker_order(o, "RECONCILE")
        async with self.sm() as s:
            still = [p for p in await repo.live_positions(s) if p.symbol == symbol and p.qty > 0]
        if not still:
            return True
        if sum(o.filled_qty for o in sells) < need:
            return False
        # Filled sells exist but are not linked to our orders: close with their average.
        total = sum(o.filled_qty for o in sells)
        notional = sum(((o.filled_avg_price or Decimal(0)) * o.filled_qty for o in sells),
                       Decimal(0))
        price = (notional / total).quantize(pm.PX)
        async with transaction(self.sm) as s:
            for p in still:
                pos = await repo.get_position(s, p.id, lock=True)
                if pos is None or pos.qty <= 0:
                    continue
                d = pm.ledger(pos)
                d["exit_qty"] += pos.qty
                d["exit_notional"] = str(Decimal(d["exit_notional"]) + price * pos.qty)
                d["reconciled_from"] = [o.id for o in sells]
                pos.avg_exit_price, pos.qty, pos.exit_detail = price, 0, d
                reason = "STOP_LOSS" if any(o.type == "stop" for o in sells) else "RECONCILE"
                await pm.close(s, pos, reason, self.clock())
        return True

    async def _adopt_broker_qty(self, positions: list[Any], broker_qty: int) -> None:
        async with transaction(self.sm) as s:
            remaining = broker_qty
            for i, p in enumerate(positions):
                pos = await repo.get_position(s, p.id, lock=True)
                if pos is None:
                    continue
                new = remaining if i == len(positions) - 1 else min(pos.qty, remaining)
                d = pm.ledger(pos)
                d["reconcile_qty"] = {"from": pos.qty, "to": new}
                pos.qty, pos.exit_detail = max(new, 0), d
                remaining -= pos.qty


def _flatten(orders: list[BrokerOrder]) -> list[BrokerOrder]:
    out: list[BrokerOrder] = []
    for o in orders:
        out.append(o)
        out.extend(_flatten(list(o.legs)))
    return out
