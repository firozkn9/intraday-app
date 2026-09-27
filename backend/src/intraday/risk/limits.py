"""Effective limits: configured values, never above the equity-relative hard limits.

Settings are validated against hard limits when saved, but equity moves; the
risk engine re-applies the caps at every evaluation (defence in depth).
"""

from __future__ import annotations

from decimal import Decimal

from intraday.core import hard_limits as hl
from intraday.core.runtime_config import TradingConfig


def effective_limits(cfg: TradingConfig, equity: Decimal) -> dict[str, Decimal]:
    r = cfg.risk
    eq = max(equity, Decimal("0"))
    return {
        "max_loss_per_trade": min(r.max_loss_per_trade,
                                  eq * hl.HARD_MAX_LOSS_PER_TRADE_PCT_OF_EQUITY),
        "max_daily_loss": min(r.max_daily_loss, eq * hl.HARD_MAX_DAILY_LOSS_PCT_OF_EQUITY),
        "max_total_exposure": min(r.max_total_exposure,
                                  eq * hl.HARD_MAX_TOTAL_EXPOSURE_MULTIPLE_OF_EQUITY),
        "max_capital_per_trade": min(r.max_capital_per_trade,
                                     eq * hl.HARD_MAX_CAPITAL_PER_TRADE_PCT_OF_EQUITY),
        "max_sector_exposure": r.max_sector_exposure,
        "max_correlated_exposure": r.max_correlated_exposure,
        "min_stock_price": max(cfg.universe.min_stock_price, hl.HARD_MIN_STOCK_PRICE),
    }
