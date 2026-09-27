"""Strategy A — Momentum Breakout (SPEC §12; docs/design/09 §4)."""

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


class MomentumBreakout:
    name = "momentum_breakout"
    label = "Momentum Breakout"

    def evaluate(self, state: SymbolState, regime: Regime, cfg: TradingConfig) -> StrategyEval:
        p = cfg.strategies.momentum_breakout
        f, s = state.features, state.series
        n = p.breakout_lookback_bars
        conds: list[Condition] = []

        conds.append(Condition("above_vwap", f.price > f.vwap,
                               f"price {f.vwap_dist_pct:+.2f}% vs VWAP", f.vwap_dist_pct))
        ema_ok = f.ema9 is not None and f.ema20 is not None and f.ema9 > f.ema20
        conds.append(Condition("ema9_gt_ema20", ema_ok,
                               "EMA 9 > EMA 20" if ema_ok else "EMA 9 not above EMA 20"))
        level = max(s.highs[-n - 1:-1]) if len(s.highs) > n else None
        brk = level is not None and f.price > level
        conds.append(Condition("breakout", brk,
                               f"close ${f.price:.2f} vs {n}-bar high ${level:.2f}"
                               if level is not None else f"needs {n + 1} bars today", level))
        conds.append(rvol_condition(f.rvol, p.min_rvol))
        need = p.min_volume_increase_bars
        conds.append(Condition("volume_rising", f.rising_volume_bars >= need,
                               f"volume rising {f.rising_volume_bars} bars (need {need})",
                               f.rising_volume_bars))
        conds.append(regime_condition(regime, cfg))

        if not p.enabled:
            conds.append(Condition("enabled", False, "strategy disabled in settings"))
        if not all(c.passed for c in conds) or level is None:
            return StrategyEval(self.name, tuple(conds))

        atr = f.atr14_1m or 0.0
        quality = ramp((f.price - level) / atr, 0.0, 1.0) if atr else 0.5
        signals = [f"Breakout above {n}-bar high (${level:.2f})",
                   f"Price above VWAP ({f.vwap_dist_pct:+.2f}%)", "EMA 9 > EMA 20",
                   f"Relative volume {f.rvol:.1f}x", f"Volume rising {f.rising_volume_bars} bars"]
        cand = build_candidate(self.name, state, cfg, method=p.stop_method,
                               atr_multiple=p.atr_multiple, max_stop_pct=p.max_stop_percent,
                               structure=level, quality=quality, signals=signals)
        return StrategyEval(self.name, tuple(conds), cand, cand.signals)
