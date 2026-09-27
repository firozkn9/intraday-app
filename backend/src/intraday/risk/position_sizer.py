"""Position sizing (docs/design/07 §3; SPEC §5). Decimal math, shares floored.

qty = min over every bound; each bound is recorded so the UI can show which one
was binding:

    risk      floor(max_loss x m / risk_per_share)
    capital   floor(max_capital x m / entry)
    exposure  floor((max_exposure - exposure - pending) / entry)
    bp        floor(buying_power x utilisation / entry)
    daily     floor(daily_loss_headroom / risk_per_share)
    sector    floor((max_sector - sector_exposure) / entry)
    corr      floor((max_correlated - cluster_exposure) / entry)

risk_per_share = (entry - stop) + entry x slippage_buffer_bps / 10 000
m = regime size multiplier (1.0, or the configured REDUCE / HIGH_VOL multipliers)
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from intraday.core.runtime_config import TradingConfig
from intraday.features.regime import Regime
from intraday.risk import daily_loss
from intraday.risk.limits import effective_limits
from intraday.risk.models import ZERO, RiskInputs, Sizing

BPS = Decimal("10000")


def _floor(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR)) if x > 0 else 0


def regime_multiplier(regime: Regime, cfg: TradingConfig) -> Decimal:
    m = Decimal("1")
    reduced = ((regime.trend == "UNKNOWN" and cfg.regime.unknown_policy == "REDUCE")
               or (regime.trend == "BEARISH" and cfg.regime.bearish_policy == "REDUCE"))
    if reduced:
        m *= Decimal(str(cfg.regime.reduce_size_multiplier))
    if regime.high_volatility:
        m *= Decimal(str(cfg.regime.high_volatility_size_multiplier))
    return m


def risk_per_share(entry: Decimal, stop: Decimal, slippage_bps: int) -> Decimal:
    return (entry - stop) + entry * Decimal(slippage_bps) / BPS


def size(inputs: RiskInputs, cfg: TradingConfig) -> Sizing:
    p, pf, acct = inputs.proposal, inputs.portfolio, inputs.account
    limits = effective_limits(cfg, acct.equity)
    rps = risk_per_share(p.entry, p.stop, cfg.risk.slippage_buffer_bps)
    m = regime_multiplier(inputs.regime, cfg)
    if p.entry <= 0 or rps <= 0:
        bounds = {"invalid_prices": 0}
        return Sizing(0, bounds, "invalid_prices", rps, m, limits)

    sector_exp = pf.sector_exposure(inputs.market.sector, p.symbol)
    bounds = {
        "risk": _floor(limits["max_loss_per_trade"] * m / rps),
        "capital": _floor(limits["max_capital_per_trade"] * m / p.entry),
        "exposure": _floor((limits["max_total_exposure"] - pf.exposure - pf.pending_exposure)
                           / p.entry),
        "buying_power": _floor(acct.buying_power
                               * Decimal(str(cfg.risk.buying_power_utilization)) / p.entry),
        "daily_loss": _floor(daily_loss.headroom(pf, limits["max_daily_loss"],
                                                 cfg.risk.daily_loss_basis) / rps),
        "sector": _floor((limits["max_sector_exposure"] - sector_exp) / p.entry),
        "correlated": _floor((limits["max_correlated_exposure"] - inputs.correlated_exposure)
                             / p.entry),
    }
    binding = min(bounds, key=lambda k: bounds[k])
    qty = max(0, bounds[binding])
    return Sizing(qty, bounds, binding, rps, m, limits)


__all__ = ["ZERO", "regime_multiplier", "risk_per_share", "size"]
