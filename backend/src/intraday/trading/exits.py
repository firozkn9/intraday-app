"""Exit rules (SPEC §14; docs/design/08 §7). Pure; shared by backtests and (Phase 8) live.

Broker-side bracket legs handle stop-loss and take-profit. The engine may exit
earlier on bar-close rules, and it only ever *raises* a trailing stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from intraday.core.runtime_config import ExitSettings
from intraday.features.feature_engineering import Features
from intraday.features.regime import Regime

HUNDRED = Decimal("100")


@dataclass(frozen=True)
class ExitSignal:
    reason: str      # TIME_EXIT | MOMENTUM_REVERSAL | VWAP_LOSS | REGIME_REVERSAL | EOD_LIQUIDATION
    detail: str


def trailing_stop(*, entry: Decimal, initial_stop: Decimal, current_stop: Decimal,
                  high_water: Decimal, cfg: ExitSettings) -> tuple[Decimal, bool]:
    """(new stop, trailing active). Activates after +activation_r x R; never lowers the stop."""
    if not cfg.trailing_enabled:
        return current_stop, False
    r = entry - initial_stop
    if r <= 0 or high_water < entry + r * Decimal(str(cfg.trailing_activation_r)):
        return current_stop, False
    trail = high_water * (1 - Decimal(str(cfg.trailing_distance_percent)) / HUNDRED)
    return max(current_stop, trail.quantize(Decimal("0.01"))), True


def bar_close_exit(*, f: Features, entry_time: datetime, now: datetime, cfg: ExitSettings,
                   regime: Regime, closes_below_vwap: int, force_close: datetime | None
                   ) -> ExitSignal | None:
    """Engine exit rules evaluated at a bar close (the exit fills at the next bar)."""
    if force_close is not None and now >= force_close:
        return ExitSignal("EOD_LIQUIDATION", "force-close time reached")
    held = now - entry_time
    if held >= timedelta(minutes=cfg.max_holding_minutes):
        return ExitSignal("TIME_EXIT", f"held {int(held.total_seconds() // 60)} min "
                                       f"(max {cfg.max_holding_minutes})")
    if (cfg.momentum_reversal_enabled and f.ema9 is not None and f.ema20 is not None
            and f.ema9_prev is not None and f.ema20_prev is not None and f.macd_hist is not None
            and f.ema9_prev >= f.ema20_prev and f.ema9 < f.ema20 and f.macd_hist < 0):
        return ExitSignal("MOMENTUM_REVERSAL", "EMA 9 crossed below EMA 20 with MACD negative")
    if cfg.vwap_loss_enabled and closes_below_vwap >= cfg.vwap_loss_bars:
        return ExitSignal("VWAP_LOSS", f"{closes_below_vwap} closes below VWAP "
                                       f"(${f.vwap:.2f})")
    if cfg.regime_reversal_enabled and (regime.trend == "BEARISH" or (
            regime.high_volatility and regime.trend != "BULLISH")):
        return ExitSignal("REGIME_REVERSAL", f"market regime turned {regime.label}")
    return None
