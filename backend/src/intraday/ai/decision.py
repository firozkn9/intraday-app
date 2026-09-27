"""Trade proposal (docs/design/09 §6). Built by code — never by an LLM (decision C2)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from intraday.ai.scoring import ScoreResult
from intraday.features.feature_engineering import Features
from intraday.features.regime import Regime
from intraday.strategies.base import Candidate

CENT = Decimal("0.01")


def _price(x: float) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


class TradeProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    action: Literal["BUY"] = "BUY"   # long only; exits come from the exit engine
    strategy: str
    confidence: float = Field(ge=0, le=1, description="= score (D2); not a probability")
    score: float = Field(ge=0, le=1)
    factor_scores: dict[str, float]
    entry_ref_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    stop_loss_pct: float
    take_profit_pct: float
    expected_return: float = Field(description="target distance, not a forecast")
    estimated_risk: float = Field(description="stop distance, not a forecast")
    risk_reward_ratio: float
    holding_period: Literal["intraday"] = "intraday"
    stop_method: str
    signals: list[str]
    market_context: dict[str, Any]
    model_version: str
    config_version: int
    bar_ts: str

    @classmethod
    def build(cls, candidate: Candidate, score: ScoreResult, features: Features,
              regime: Regime, config_version: int) -> TradeProposal:
        entry, stop, target = (_price(candidate.entry_ref), _price(candidate.stop),
                               _price(candidate.target))
        risk = entry - stop
        rr = float((target - entry) / risk) if risk > 0 else 0.0
        return cls(
            symbol=features.symbol, strategy=candidate.strategy, confidence=score.score,
            score=score.score, factor_scores=score.factor_scores, entry_ref_price=entry,
            stop_loss=stop, take_profit=target,
            stop_loss_pct=round(float(risk / entry), 6),
            take_profit_pct=round(float((target - entry) / entry), 6),
            expected_return=round(float((target - entry) / entry), 6),
            estimated_risk=round(float(risk / entry), 6), risk_reward_ratio=round(rr, 3),
            stop_method=candidate.stop_method, signals=list(candidate.signals),
            market_context=regime.to_json(), model_version=score.model_version,
            config_version=config_version, bar_ts=features.bar_ts)
