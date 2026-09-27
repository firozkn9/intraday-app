"""Deterministic trade scoring (SPEC §13; docs/design/09 §5; decisions D1, D2).

Every factor maps features to a sub-score in [0, 1] with documented, monotone,
piecewise-linear functions; the score is the configured weighted sum. The
score is shown as "model confidence" — it is a score, not a probability of
profit. An ML scorer can later implement the same ``Scorer`` protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from intraday.core.runtime_config import ScoringSettings
from intraday.features.feature_engineering import Features
from intraday.features.regime import Regime
from intraday.strategies.base import Candidate, ramp

MODEL_VERSION = "weighted-rule-v1"

REGIME_SCORE = {"BULLISH": 1.0, "SIDEWAYS": 0.5, "LOW_VOLATILITY": 0.5,
                "HIGH_VOLATILITY": 0.3, "BEARISH": 0.0, "UNKNOWN": 0.0}
NEWS_UNKNOWN = 0.5   # neutral until news arrives (Phase 9; decision G4)


@dataclass(frozen=True)
class ScoreResult:
    score: float
    factor_scores: dict[str, float]
    model_version: str = MODEL_VERSION


class Scorer(Protocol):
    def score(self, features: Features, candidate: Candidate, regime: Regime,
              weights: ScoringSettings, news: float = ...) -> ScoreResult: ...


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def momentum_score(f: Features) -> float:
    ema = 1.0 if f.ema9 is not None and f.ema20 is not None and f.ema9 > f.ema20 else 0.0
    if f.macd_hist is None:
        macd = 0.0
    elif f.macd_hist > 0 and f.macd_hist_prev is not None and f.macd_hist >= f.macd_hist_prev:
        macd = 1.0
    else:
        macd = 0.5 if f.macd_hist > 0 else 0.0
    rsi = 0.0
    if f.rsi14 is not None:
        # Best in the 50-70 band; fades to 0 at 40 and at 80 (overbought).
        rsi = ramp(f.rsi14, 40, 50) if f.rsi14 < 50 else 1.0 - ramp(f.rsi14, 70, 80)
    return _mean([ema, macd, ramp(f.roc10, 0.0, 1.0), rsi])


def volume_score(f: Features) -> float:
    trend = ramp(f.volume_trend, 0.0, 0.1)
    return _mean([ramp(f.rvol, 1.0, 3.0), ramp(f.volume_spike, 1.0, 3.0), trend])


def volatility_score(f: Features) -> float:
    """1-min ATR% sweet spot 0.08-0.40%; too quiet or too wild both penalised."""
    a = f.atr_pct
    if a is None:
        return 0.0
    if a < 0.08:
        return ramp(a, 0.02, 0.08)
    return 1.0 - ramp(a, 0.40, 1.00)


def technical_score(f: Features) -> float:
    """Penalise extension from VWAP (in 1-min ATRs) and entries far above the day's range."""
    if f.atr14_1m:
        ext = abs(f.price - f.vwap) / f.atr14_1m
        extension = 1.0 - ramp(ext, 1.0, 4.0)
    else:
        extension = 0.5
    to_high = (f.day_high - f.price) / f.price * 100 if f.price else 0.0
    headroom = 1.0 if to_high <= 0.05 else 0.7  # at/through the high = no overhead supply
    return _mean([extension, headroom])


class WeightedRuleScorer:
    def score(self, features: Features, candidate: Candidate, regime: Regime,
              weights: ScoringSettings, news: float = 0.5) -> ScoreResult:
        factors = {
            "momentum": momentum_score(features),
            "volume": volume_score(features),
            "price_action": candidate.quality,
            "market_regime": REGIME_SCORE[regime.label],
            "volatility": volatility_score(features),
            "technical": technical_score(features),
            "news": news if news is not None else NEWS_UNKNOWN,
        }
        w = weights.weights
        total = sum(w[k] * v for k, v in factors.items())
        return ScoreResult(round(max(0.0, min(1.0, total)), 4),
                           {k: round(v, 4) for k, v in factors.items()})
