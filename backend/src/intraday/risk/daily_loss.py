"""Daily loss (docs/design/07 §4; SPEC §6)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from intraday.risk.models import ZERO, PortfolioSnapshot


@dataclass(frozen=True)
class DailyLossStatus:
    daily_pnl: Decimal       # realized (net) + unrealized mark-to-market
    loss_used: Decimal       # max(0, -daily_pnl)
    limit: Decimal
    breached: bool

    @property
    def remaining(self) -> Decimal:
        return max(ZERO, self.limit - self.loss_used)


def status(portfolio: PortfolioSnapshot, limit: Decimal) -> DailyLossStatus:
    pnl = portfolio.daily_pnl
    used = max(ZERO, -pnl)
    return DailyLossStatus(pnl, used, limit, used >= limit)


def headroom(portfolio: PortfolioSnapshot, limit: Decimal, basis: str) -> Decimal:
    """Loss budget left for a new trade's risk.

    WORST_CASE_OPEN_RISK: also reserve what every open stop (and pending entry)
    could still lose, so even all stops hitting at once can't breach the limit.
    CURRENT_PNL: only the loss already taken.
    """
    used = max(ZERO, -portfolio.daily_pnl)
    reserved = portfolio.open_risk if basis == "WORST_CASE_OPEN_RISK" else ZERO
    return limit - used - reserved
