"""Liquidator: force close, emergency stop, close-all and daily-loss flatten (08 §8).

One procedure for all of them: record the reason on local positions, cancel
every open order at the broker (entries and protective legs), confirm, then
market-sell every long position the **broker** reports (not only the ones
known locally). Short positions can't exist in a long-only account; one is
never traded, only alerted.

End of day runs this at ``force_close`` and verifies flat every 10 s, then
every 15 s; not flat 3 minutes later → HIGH alert, 30 s before the close →
CRITICAL. At 16:15 a final check raises a CRITICAL overnight-position alert.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.logging import get_logger
from intraday.core.timeutil import utcnow
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import trading as repo
from intraday.db.session import transaction
from intraday.marketdata.calendar import EntryWindow
from intraday.trading.broker.base import Broker, BrokerError
from intraday.trading.order_manager import TERMINAL, OrderManager

log = get_logger(__name__)

HIGH_AFTER = timedelta(minutes=3)
CRITICAL_BEFORE_CLOSE = timedelta(seconds=30)
FAST_RETRY = timedelta(seconds=10)
SLOW_RETRY = timedelta(seconds=15)


@dataclass
class LiquidationResult:
    reason: str
    canceled: int = 0
    submitted: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"reason": self.reason, "canceled": self.canceled, "submitted": self.submitted,
                "failed": self.failed, "skipped": self.skipped, "error": self.error}


@dataclass
class EodState:
    session_date: date
    started: bool = False
    last_attempt: datetime | None = None
    high_sent: bool = False
    critical_sent: bool = False
    done: bool = False


class Liquidator:
    def __init__(self, sm: async_sessionmaker[AsyncSession], broker: Broker, om: OrderManager,
                 *, clock: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.sm = sm
        self.broker = broker
        self.om = om
        self.clock = clock
        self.sleep = sleep
        self.eod: EodState | None = None
        self._lock = asyncio.Lock()

    async def liquidate(self, reason: str, intent: str) -> LiquidationResult:
        async with self._lock:
            return await self._liquidate(reason, intent)

    async def _liquidate(self, reason: str, intent: str) -> LiquidationResult:
        result = LiquidationResult(reason)
        async with self.sm() as s:
            live = await repo.live_positions(s)
            pending_exits = {o.symbol for o in await repo.non_terminal_orders(s)
                             if o.intent in repo.EXIT_INTENTS and o.status not in TERMINAL}
        await self.om.mark_exiting([p.id for p in live], reason)
        try:
            open_orders = await self.broker.get_open_orders()
            ids: list[str] = []
            for o in open_orders:
                for x in (o, *o.legs):   # everything but exit market sells already working
                    if not x.terminal and not (x.side == "sell" and x.type == "market"):
                        ids.append(x.id)
            for bid in ids:
                try:
                    await self.broker.cancel_order(bid)
                    result.canceled += 1
                except BrokerError as exc:
                    if exc.status not in (404, 422):
                        log.warning("liquidation_cancel_failed", order=bid, error=exc.message)
            await self.om.confirm_terminal(ids)
            positions = await self.broker.get_positions()
        except BrokerError as exc:
            result.error = exc.message
            log.error("liquidation_broker_error", reason=reason, error=exc.message)
            return result

        by_symbol = {p.symbol: p for p in live}
        for bp in positions:
            if bp.qty < 0:
                result.skipped.append(bp.symbol)
                await self._alert("CRITICAL", "SHORT_POSITION",
                                  f"{bp.symbol}: broker reports a short position ({bp.qty}); "
                                  "the engine never trades it. Close it in the Alpaca dashboard.",
                                  f"short:{bp.symbol}")
                continue
            if bp.qty == 0 or bp.symbol in pending_exits:
                continue
            local = by_symbol.get(bp.symbol)
            status = await self.om.submit_sell(bp.symbol, bp.qty, intent, reason,
                                               local.id if local else None)
            entry = {"symbol": bp.symbol, "qty": bp.qty, "status": status,
                     "known_locally": local is not None}
            (result.submitted if status == "SUBMITTED" else result.failed).append(entry)
        async with transaction(self.sm) as s:
            await engine_repo.system_event(
                s, component="trading", type="LIQUIDATION", severity="WARN",
                message=f"Liquidation ({reason}): {len(result.submitted)} sell orders, "
                        f"{len(result.failed)} failed", details=result.to_json())
        log.warning("liquidation", **result.to_json())
        return result

    async def is_flat(self) -> bool:
        return not any(p.qty != 0 for p in await self.broker.get_positions())

    async def run_until_flat(self, reason: str, intent: str, *,
                             deadline: timedelta = timedelta(minutes=3)) -> bool:
        """Close-all / emergency / flatten: liquidate, verify every 10 s, escalate."""
        start = self.clock()
        high_sent = False
        while True:
            await self.liquidate(reason, intent)
            for _ in range(10):
                await self.sleep(1.0)
                try:
                    if await self.is_flat():
                        await self._event("FLAT_VERIFIED", f"Flat after {reason}")
                        return True
                except BrokerError:
                    continue
            if self.clock() - start >= deadline:
                await self._alert("CRITICAL", "NOT_FLAT",
                                  f"Still holding positions after {reason}; check the broker",
                                  f"not-flat:{reason}:{start:%Y%m%d}")
                return False
            if not high_sent:
                await self._alert("HIGH", "NOT_FLAT", f"Not flat yet after {reason}; retrying",
                                  f"not-flat:{reason}:{start:%Y%m%d}")
                high_sent = True

    async def eod_tick(self, now: datetime, window: EntryWindow) -> None:
        """Called every few seconds; drives force close, verification and escalation."""
        day = window.session.session_date
        if self.eod is None or self.eod.session_date != day:
            self.eod = EodState(day)
        st = self.eod
        if st.done or now < window.force_close:
            return
        try:
            flat = await self.is_flat()
        except BrokerError as exc:
            log.warning("eod_flat_check_failed", error=exc.message)
            flat = False
        if now >= window.session.close_at:
            if not flat and not st.critical_sent:
                await self._alert("CRITICAL", "NOT_FLAT_AT_CLOSE",
                                  "Positions still open at the market close",
                                  f"eod-critical:{day}")
            st.done = True
            return
        if flat and st.started:
            await self._event("EOD_FLAT", f"Flat after force close ({day})")
            st.done = True
            return
        interval = FAST_RETRY if now < window.force_close + HIGH_AFTER else SLOW_RETRY
        if not st.started or st.last_attempt is None or now - st.last_attempt >= interval:
            await self.liquidate("EOD_LIQUIDATION", "FORCE_CLOSE")
            st.started, st.last_attempt = True, now
        if flat:
            return
        if now >= window.force_close + HIGH_AFTER and not st.high_sent:
            await self._alert("HIGH", "NOT_FLAT", "Not flat 3 minutes after force close; "
                              "retrying every 15 s", f"eod-high:{day}")
            st.high_sent = True
        if now >= window.session.close_at - CRITICAL_BEFORE_CLOSE and not st.critical_sent:
            await self._alert("CRITICAL", "NOT_FLAT", "Still not flat 30 s before the close",
                              f"eod-critical:{day}")
            st.critical_sent = True

    async def overnight_check(self) -> list[str]:
        """16:15 ET: anything still held at the broker is an overnight position."""
        held = [f"{p.symbol} x{p.qty}" for p in await self.broker.get_positions() if p.qty]
        if held:
            await self._alert("CRITICAL", "OVERNIGHT_POSITION",
                              f"Positions held after the close: {', '.join(held)}",
                              f"overnight:{self.clock():%Y%m%d}")
        return held

    async def _alert(self, severity: str, type_: str, message: str, dedupe: str) -> None:
        log.error("liquidation_alert", severity=severity, type=type_, message=message)
        async with transaction(self.sm) as s:
            await engine_repo.raise_alert(s, severity=severity, type=type_, message=message,
                                          dedupe_key=dedupe)

    async def _event(self, type_: str, message: str) -> None:
        async with transaction(self.sm) as s:
            await engine_repo.system_event(s, component="trading", type=type_, severity="INFO",
                                           message=message)
