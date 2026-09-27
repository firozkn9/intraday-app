"""Market regime detector (docs/design/09 §3). Pure.

Inputs: SPY and QQQ features at the current bar, plus breadth (share of the
scanned universe above VWAP). Output: a trend label, a high-volatility overlay,
and whether the regime policy allows new long entries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from intraday.core.runtime_config import RegimeSettings
from intraday.features.feature_engineering import Features

Label = Literal["BULLISH", "BEARISH", "SIDEWAYS", "HIGH_VOLATILITY", "LOW_VOLATILITY", "UNKNOWN"]
Trend = Literal["BULLISH", "BEARISH", "SIDEWAYS", "UNKNOWN"]

REFERENCE = ("SPY", "QQQ")
WARMUP_MINUTES = 5            # regime is UNKNOWN before 09:35 ET
HIGH_VOL_PERCENTILE = 90.0
LOW_VOL_PERCENTILE = 20.0
BREADTH_BULL = 0.55
BREADTH_BEAR = 0.45


@dataclass(frozen=True)
class Regime:
    label: Label
    trend: Trend
    high_volatility: bool
    vol_percentile: float | None
    breadth: float | None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "trend": self.trend, "high_volatility": self.high_volatility,
                "vol_percentile": None if self.vol_percentile is None
                else round(self.vol_percentile, 1),
                "breadth": None if self.breadth is None else round(self.breadth, 3),
                "reasons": list(self.reasons)}


UNKNOWN = Regime("UNKNOWN", "UNKNOWN", False, None, None, ("no market data",))


def breadth(universe: Sequence[Features]) -> float | None:
    if not universe:
        return None
    return sum(1 for f in universe if f.price > f.vwap) / len(universe)


def detect(reference: Mapping[str, Features | None], universe: Sequence[Features]) -> Regime:
    refs = [reference.get(s) for s in REFERENCE]
    missing = [s for s, f in zip(REFERENCE, refs, strict=True) if f is None]
    if missing:
        return Regime("UNKNOWN", "UNKNOWN", False, None, breadth(universe),
                      (f"no data for {', '.join(missing)}",))
    spy, qqq = refs[0], refs[1]
    assert spy is not None and qqq is not None
    if spy.minute_index < WARMUP_MINUTES:
        return Regime("UNKNOWN", "UNKNOWN", False, None, breadth(universe),
                      ("warming up after the open",))
    if spy.ema9 is None or spy.ema20 is None or qqq.ema9 is None or qqq.ema20 is None:
        return Regime("UNKNOWN", "UNKNOWN", False, None, breadth(universe),
                      ("indicators not warmed up",))

    b = breadth(universe)
    up = all(f.price > f.vwap and f.ema9 > f.ema20 for f in (spy, qqq))  # type: ignore[operator]
    down = all(f.price < f.vwap and f.ema9 < f.ema20 for f in (spy, qqq))  # type: ignore[operator]
    reasons = [f"SPY {'above' if spy.price > spy.vwap else 'below'} VWAP "
               f"({spy.vwap_dist_pct:+.2f}%)",
               f"QQQ {'above' if qqq.price > qqq.vwap else 'below'} VWAP "
               f"({qqq.vwap_dist_pct:+.2f}%)"]
    if b is not None:
        reasons.append(f"breadth {b:.0%}")
    if up and (b is None or b >= BREADTH_BULL):
        trend: Trend = "BULLISH"
    elif down and (b is None or b <= BREADTH_BEAR):
        trend = "BEARISH"
    else:
        trend = "SIDEWAYS"

    vp = spy.rv_percentile
    high_vol = vp is not None and vp >= HIGH_VOL_PERCENTILE
    if vp is not None:
        reasons.append(f"SPY 30-min volatility at the {vp:.0f}th percentile")
    if high_vol:
        label: Label = "HIGH_VOLATILITY"
    elif trend == "SIDEWAYS" and vp is not None and vp <= LOW_VOL_PERCENTILE:
        label = "LOW_VOLATILITY"
    else:
        label = trend
    return Regime(label, trend, high_vol, vp, b, tuple(reasons))


def entries_allowed(regime: Regime, policy: RegimeSettings) -> tuple[bool, str]:
    """Hard regime gate for new longs (sizing multipliers apply later, in the risk engine)."""
    if regime.trend == "UNKNOWN" and policy.unknown_policy == "DISABLE":
        return False, "regime UNKNOWN (policy: DISABLE)"
    if regime.trend == "BEARISH" and policy.bearish_policy == "DISABLE":
        return False, "regime BEARISH (policy: DISABLE, long only)"
    return True, f"regime {regime.label}"
