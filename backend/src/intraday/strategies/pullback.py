"""Strategy C — Pullback in an established intraday trend (SPEC §12; docs/design/09 §4)."""

from __future__ import annotations

from intraday.core.runtime_config import TradingConfig
from intraday.features.feature_engineering import SymbolState
from intraday.features.regime import Regime
from intraday.strategies.base import (
    Condition,
    StrategyEval,
    build_candidate,
    ramp,
    regime_condition,
    rvol_condition,
)

MIN_PULLBACK_PCT = 0.1       # below this there was no real pullback
TOUCH_BARS = 3               # support must be touched within the last N bars


class Pullback:
    name = "pullback"
    label = "Pullback"

    def evaluate(self, state: SymbolState, regime: Regime, cfg: TradingConfig) -> StrategyEval:
        p = cfg.strategies.pullback
        f, s = state.features, state.series
        conds: list[Condition] = []

        n = p.trend_min_bars
        aligned = len(s.ema50) >= n and all(
            a is not None and b is not None and c is not None and a > b > c
            for a, b, c in zip(s.ema9[-n:], s.ema20[-n:], s.ema50[-n:], strict=True))
        conds.append(Condition("established_trend", aligned and f.price > f.vwap,
                               f"EMA 9 > 20 > 50 for {n} bars and price above VWAP"
                               if aligned else f"EMAs not aligned for {n} bars", None))

        window_high = max(s.highs[-n:]) if len(s.highs) >= 1 else f.price
        recent_low = min(s.lows[-5:])
        depth = (window_high - recent_low) / window_high * 100 if window_high else 0.0
        depth_ok = MIN_PULLBACK_PCT <= depth <= p.max_pullback_percent
        conds.append(Condition("controlled_pullback", depth_ok,
                               f"pullback {depth:.2f}% from ${window_high:.2f} (allowed "
                               f"{MIN_PULLBACK_PCT}-{p.max_pullback_percent}%)", depth))

        support = f.ema20 if p.support == "EMA20" else f.vwap
        tol = p.support_tolerance_percent / 100
        touched = support is not None and any(
            abs(low - support) / support <= tol or low <= support
            for low in s.lows[-TOUCH_BARS:])
        conds.append(Condition("support_touch", touched,
                               f"low within {p.support_tolerance_percent}% of {p.support} "
                               f"(${support:.2f})" if support is not None
                               else f"{p.support} unavailable", support))
        vol_ok = f.bar_green and f.volume_spike is not None and f.volume_spike >= 1.0
        conds.append(Condition("bounce_confirmed", vol_ok,
                               "green bar on above-average volume" if vol_ok
                               else "no green bar on above-average volume", f.volume_spike))
        conds.append(rvol_condition(f.rvol, p.min_rvol))
        not_bear = regime.trend != "BEARISH"
        conds.append(Condition("market_not_bearish", not_bear, f"market trend {regime.trend}"))
        conds.append(regime_condition(regime, cfg))
        if not p.enabled:
            conds.append(Condition("enabled", False, "strategy disabled in settings"))
        if not all(c.passed for c in conds) or support is None:
            return StrategyEval(self.name, tuple(conds))

        quality = (ramp(p.max_pullback_percent - depth, 0, p.max_pullback_percent)
                   + ramp(f.volume_spike, 1.0, 2.0)) / 2
        signals = [f"Uptrend: EMA 9 > EMA 20 > EMA 50 for {n}+ bars",
                   f"Controlled pullback {depth:.2f}% to {p.support} (${support:.2f})",
                   "Bounce on above-average volume", f"Relative volume {f.rvol:.1f}x"]
        cand = build_candidate(self.name, state, cfg, method=p.stop_method,
                               atr_multiple=p.atr_multiple, max_stop_pct=p.max_stop_percent,
                               structure=min(support, recent_low), quality=quality,
                               signals=signals)
        return StrategyEval(self.name, tuple(conds), cand, cand.signals)
