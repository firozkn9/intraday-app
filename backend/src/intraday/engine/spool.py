"""Trade-update spool for database outages (docs/design/01 §6).

Broker trade updates keep arriving while the database is down. They are held
in memory, in order, and replayed once the database is back. Applying an
update is idempotent (fills are keyed by execution id and by filled_qty), so a
replay after a partial failure is safe. If the spool overflows, the oldest
updates are dropped and ``overflowed`` is set: reconciliation against the
broker then restores the state, which is why a reconcile always follows a
database recovery.
"""

from __future__ import annotations

from collections import deque

from sqlalchemy.exc import SQLAlchemyError

from intraday.core.logging import get_logger
from intraday.trading.broker.base import TradeUpdate, TradeUpdateHandler

log = get_logger(__name__)

DB_ERRORS = (SQLAlchemyError, OSError)


class TradeUpdateSpool:
    def __init__(self, handler: TradeUpdateHandler, *, maxlen: int = 10_000) -> None:
        self.handler = handler
        self.items: deque[TradeUpdate] = deque(maxlen=maxlen)
        self.overflowed = False

    def __len__(self) -> int:
        return len(self.items)

    async def handle(self, u: TradeUpdate) -> None:
        if self.items:                      # keep order: queue behind older updates
            self._push(u)
            return
        try:
            await self.handler(u)
        except DB_ERRORS as exc:
            log.error("trade_update_spooled", error=type(exc).__name__, order=u.order.id,
                      update=u.event)
            self._push(u)

    def _push(self, u: TradeUpdate) -> None:
        if len(self.items) == self.items.maxlen:
            self.overflowed = True
        self.items.append(u)

    async def drain(self) -> int:
        """Replay spooled updates in order; stops (keeping the rest) on a DB error."""
        n = 0
        while self.items:
            try:
                await self.handler(self.items[0])
            except DB_ERRORS:
                break
            self.items.popleft()
            n += 1
        if n:
            log.info("trade_updates_replayed", count=n, remaining=len(self.items))
        return n
