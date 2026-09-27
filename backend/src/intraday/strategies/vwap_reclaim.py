"""Strategy B — VWAP Reclaim (SPEC §12; docs/design/09 §4)."""

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


class VwapReclaim:
    name = "vwap_reclaim"
    label = "VWAP Reclaim"

    def evaluate(self, state: SymbolState, regime: Regime, cfg: TradingConfig) -> StrategyEval:
        p = cfg.strategies.vwap_reclaim
        f = state.features
        conds: list[Condition] = []

        conds.append(Condition("was_below_vwap", f.bars_below_vwap_before >= p.min_bars_below_vwap,
                               f"{f.bars_below_vwap_before} closes below VWAP before the reclaim "
                               f"(need {p.min_bars_below_vwap})", f.bars_below_vwap_before))
        conds.append(Condition("reclaimed_vwap", f.closes_above_vwap >= p.reclaim_confirm_bars,
                               f"{f.closes_above_vwap} close(s) back above VWAP "
                               f"(need {p.reclaim_confirm_bars})", f.closes_above_vwap))
        vol_ok = f.volume_spike is not None and f.volume_spike >= 1.0
        conds.append(Condition("volume_above_average", vol_ok,
                               f"bar volume {f.volume_spike:.2f}x its 20-bar average"
                               if f.volume_spike is not None else "no volume baseline",
                               f.volume_spike))
        conds.append(rvol_condition(f.rvol, p.min_rvol))
        rsi_ok = f.rsi14 is not None and f.rsi14 > p.require_rsi_above
        macd_ok = (f.macd_hist is not None and f.macd_hist_prev is not None
                   and f.macd_hist > f.macd_hist_prev)
        conds.append(Condition("momentum_confirmed", rsi_ok or macd_ok,
                               f"RSI {f.rsi14:.1f} (need > {p.require_rsi_above:g})"
                               + (" / MACD histogram rising" if macd_ok else "")
                               if f.rsi14 is not None else "RSI not warmed up", f.rsi14))
        conds.append(regime_condition(regime, cfg))
        if not p.enabled:
            conds.append(Condition("enabled", False, "strategy disabled in settings"))
        if not all(c.passed for c in conds):
            return StrategyEval(self.name, tuple(conds))

        quality = (ramp(f.bars_below_vwap_before, p.min_bars_below_vwap,
                        p.min_bars_below_vwap * 3) + ramp(f.volume_spike, 1.0, 2.5)) / 2
        signals = [f"Price reclaimed VWAP ({f.vwap_dist_pct:+.2f}%) after "
                   f"{f.bars_below_vwap_before} bars below",
                   f"Relative volume {f.rvol:.1f}x", f"Bar volume {f.volume_spike:.1f}x average"]
        if rsi_ok:
            signals.append(f"RSI {f.rsi14:.0f}")
        if macd_ok:
            signals.append("MACD histogram rising")
        cand = build_candidate(self.name, state, cfg, method=p.stop_method,
                               atr_multiple=p.atr_multiple, max_stop_pct=p.max_stop_percent,
                               structure=f.vwap, quality=quality, signals=signals)
        return StrategyEval(self.name, tuple(conds), cand, cand.signals)
