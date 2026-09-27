"""Strategy contract, conditions, and stop/target placement (docs/design/09 §4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from intraday.core.hard_limits import HARD_MAX_STOP_LOSS_PERCENT, HARD_MIN_STOP_LOSS_PERCENT
from intraday.core.runtime_config import TradingConfig
from intraday.features.feature_engineering import SymbolState
from intraday.features.regime import Regime, entries_allowed

STRUCTURE_BUFFER_PCT = 0.05   # stop sits slightly below the structural level


@dataclass(frozen=True)
class Condition:
    name: str
    passed: bool
    detail: str
    value: Any = None

    def to_json(self) -> dict[str, Any]:
        v = round(self.value, 6) if isinstance(self.value, float) else self.value
        return {"passed": self.passed, "detail": self.detail, "value": v}


@dataclass(frozen=True)
class Candidate:
    strategy: str
    entry_ref: float
    stop: float
    target: float
    stop_method: str
    stop_detail: str
    structure_level: float | None
    quality: float                   # 0..1, strategy-specific price-action quality
    signals: tuple[str, ...]

    @property
    def stop_pct(self) -> float:
        return (self.entry_ref - self.stop) / self.entry_ref * 100

    @property
    def target_pct(self) -> float:
        return (self.target - self.entry_ref) / self.entry_ref * 100

    @property
    def risk_reward(self) -> float:
        risk = self.entry_ref - self.stop
        return (self.target - self.entry_ref) / risk if risk > 0 else 0.0


@dataclass(frozen=True)
class StrategyEval:
    strategy: str
    conditions: tuple[Condition, ...]
    candidate: Candidate | None = None
    signals: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.candidate is not None

    def conditions_json(self) -> dict[str, Any]:
        return {c.name: c.to_json() for c in self.conditions}


class Strategy(Protocol):
    name: str
    label: str

    def evaluate(self, state: SymbolState, regime: Regime, cfg: TradingConfig) -> StrategyEval:
        ...


def ramp(x: float | None, lo: float, hi: float) -> float:
    """0 at/below lo, 1 at/above hi, linear between; None → 0."""
    if x is None:
        return 0.0
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def regime_condition(regime: Regime, cfg: TradingConfig) -> Condition:
    ok, detail = entries_allowed(regime, cfg.regime)
    return Condition("regime_allows", ok, detail, regime.label)


def rvol_condition(rvol: float | None, minimum: float) -> Condition:
    if rvol is None:
        return Condition("rvol", False, "relative volume unavailable (needs ≥10 sessions of "
                         "history)", None)
    return Condition("rvol", rvol >= minimum, f"RVOL {rvol:.2f}x vs minimum {minimum:.2f}x", rvol)


def place_stop(entry: float, method: str, cfg: TradingConfig, *, atr: float | None,
               atr_multiple: float, max_stop_pct: float, structure: float | None
               ) -> tuple[float, str, str]:
    """Stop price, method actually used, and a human detail. Always inside hard bounds."""
    lo = HARD_MIN_STOP_LOSS_PERCENT
    hi = min(max_stop_pct, HARD_MAX_STOP_LOSS_PERCENT)
    default_pct = cfg.exits.default_stop_loss_percent
    used, pct, detail = "PERCENT", default_pct, f"{default_pct:.2f}% below entry"
    if method == "ATR" and atr:
        used, pct = "ATR", atr_multiple * atr / entry * 100
        detail = f"{atr_multiple}x ATR(14, 1-min) = {pct:.2f}%"
    elif method == "STRUCTURE" and structure is not None and structure < entry:
        used = "STRUCTURE"
        pct = (entry - structure * (1 - STRUCTURE_BUFFER_PCT / 100)) / entry * 100
        detail = f"below structure ${structure:.2f} = {pct:.2f}%"
    elif method != "PERCENT":
        detail = f"{method} unavailable; fell back to {default_pct:.2f}%"
    clamped = min(max(pct, lo), hi)
    if clamped != pct:
        detail += f" (clamped to {clamped:.2f}%)"
    return entry * (1 - clamped / 100), used, detail


def build_candidate(strategy: str, state: SymbolState, cfg: TradingConfig, *, method: str,
                    atr_multiple: float, max_stop_pct: float, structure: float | None,
                    quality: float, signals: list[str]) -> Candidate:
    f = state.features
    entry = f.price
    stop, used, detail = place_stop(entry, method, cfg, atr=f.atr14_1m,
                                    atr_multiple=atr_multiple, max_stop_pct=max_stop_pct,
                                    structure=structure)
    target = entry * (1 + cfg.exits.default_take_profit_percent / 100)
    return Candidate(strategy, entry, stop, target, used, detail, structure,
                     max(0.0, min(1.0, quality)), tuple(signals))
