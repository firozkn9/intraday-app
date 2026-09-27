"""Live exit engine (SPEC §14; docs/design/08 §7).

Runs at every bar close on OPEN positions, with the same rules as backtests
(``trading/exits.py``): the trailing stop is raised at the broker (never
lowered), and bar-close rules (time, momentum reversal, VWAP loss, regime
reversal) trigger the engine exit procedure. Stop-loss and take-profit stay
with the broker's bracket legs; force close belongs to the Liquidator.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.logging import get_logger
from intraday.core.runtime_config import TradingConfig
from intraday.core.timeutil import utcnow
from intraday.db.repositories import trading as repo
from intraday.db.session import transaction
from intraday.features.feature_engineering import SymbolContext, compute_features
from intraday.features.regime import Regime
from intraday.marketdata.calendar import SessionDay
from intraday.marketdata.types import BAR_INTERVAL, Bar
from intraday.trading.exits import bar_close_exit, trailing_stop
from intraday.trading.order_manager import OrderManager

log = get_logger(__name__)


@dataclass(frozen=True)
class ExitAction:
    symbol: str
    kind: str        # STOP_RAISED | TRAIL_ACTIVATED | EXIT
    detail: str


def closes_below_vwap(closes: Sequence[float], vwaps: Sequence[float]) -> int:
    n = 0
    for c, v in zip(reversed(closes), reversed(vwaps), strict=False):
        if c < v:
            n += 1
        else:
            break
    return n


class ExitEngine:
    def __init__(self, sm: async_sessionmaker[AsyncSession], om: OrderManager, *,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.sm = sm
        self.om = om
        self.clock = clock

    async def evaluate(self, *, bar_ts: datetime, bars: Mapping[str, Sequence[Bar]],
                       contexts: Mapping[str, SymbolContext], session: SessionDay,
                       regime: Regime, cfg: TradingConfig) -> list[ExitAction]:
        async with self.sm() as s:
            positions = [p for p in await repo.live_positions(s)
                         if p.status == "OPEN" and p.qty > 0]
        actions: list[ExitAction] = []
        close_time = bar_ts + BAR_INTERVAL
        for pos in positions:
            seen = [b for b in bars.get(pos.symbol, ()) if b.ts <= bar_ts]
            if not seen or pos.entry_time is None:
                continue
            since_entry = [b for b in seen if b.ts + BAR_INTERVAL > pos.entry_time]
            actions += await self._trail(pos, since_entry, cfg)
            if seen[-1].ts != bar_ts:
                continue   # no bar this minute: bar-close rules wait for a real bar
            state = compute_features(seen, contexts.get(pos.symbol, SymbolContext(pos.symbol)),
                                     session)
            if state is None:
                continue
            sig = bar_close_exit(f=state.features, entry_time=pos.entry_time, now=close_time,
                                 cfg=cfg.exits, regime=regime,
                                 closes_below_vwap=closes_below_vwap(state.series.closes,
                                                                     state.series.vwaps),
                                 force_close=None)
            if sig is None:
                continue
            outcome = await self.om.exit_position(pos.id, sig.reason)
            actions.append(ExitAction(pos.symbol, "EXIT", f"{sig.reason}: {sig.detail} "
                                                          f"-> {outcome}"))
            log.info("engine_exit", symbol=pos.symbol, reason=sig.reason, detail=sig.detail,
                     outcome=outcome)
        return actions

    async def _trail(self, pos: Any, bars: Sequence[Bar], cfg: TradingConfig
                     ) -> list[ExitAction]:
        if not bars or pos.avg_entry_price is None or pos.initial_stop is None:
            return []
        high = max(Decimal(str(b.high)) for b in bars)
        high_water = max(Decimal(pos.high_water_price or 0), high)
        current = Decimal(pos.stop_price or pos.initial_stop)
        new_stop, active = trailing_stop(entry=Decimal(pos.avg_entry_price),
                                         initial_stop=Decimal(pos.initial_stop),
                                         current_stop=current, high_water=high_water,
                                         cfg=cfg.exits)
        actions: list[ExitAction] = []
        async with transaction(self.sm) as s:
            p = await repo.get_position(s, pos.id, lock=True)
            if p is None:
                return []
            p.high_water_price = high_water
            if active and not p.trailing_active:
                p.trailing_active = True
                await repo.position_event(s, p.id, "TRAIL_ACTIVATED",
                                          {"high_water": str(high_water)})
                actions.append(ExitAction(p.symbol, "TRAIL_ACTIVATED", str(high_water)))
        if new_stop > current and await self.om.raise_stop(pos.id, new_stop):
            actions.append(ExitAction(pos.symbol, "STOP_RAISED", f"{current} -> {new_stop}"))
        return actions
