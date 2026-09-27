"""Signal pipeline: one bar-close cycle (docs/design/08 §3, 09 §1). Pure.

bars → features → regime → strategies A/B/C → score → proposals.

Used by the live engine, the offline ``intraday signals`` replay, and (Phase 6)
backtests, so every path runs the same code. No look-ahead: any bar later than
the cycle's bar is dropped before features are computed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from intraday.ai.decision import TradeProposal
from intraday.ai.explain import entry_explanation
from intraday.ai.scoring import Scorer, WeightedRuleScorer
from intraday.core.runtime_config import TradingConfig
from intraday.features.feature_engineering import (
    Features,
    SymbolContext,
    SymbolState,
    compute_features,
)
from intraday.features.regime import REFERENCE, Regime, detect
from intraday.marketdata.calendar import SessionDay
from intraday.marketdata.types import Bar
from intraday.strategies import ALL_STRATEGIES, Strategy


@dataclass
class SignalRecord:
    symbol: str
    strategy: str
    bar_ts: datetime
    features: dict[str, Any]
    conditions: dict[str, Any]
    factor_scores: dict[str, float]
    score: float
    regime: str
    signals: list[str]
    action: str                          # BUY | NO_TRADE
    proposal: TradeProposal | None = None
    explanation: str | None = None
    note: str | None = None
    llm_verdict: str | None = None       # VETO mode: APPROVE | REJECT | ERROR
    llm_call_id: Any = None


@dataclass
class CycleResult:
    bar_ts: datetime
    regime: Regime
    evaluated: list[str] = field(default_factory=list)
    records: list[SignalRecord] = field(default_factory=list)

    @property
    def proposals(self) -> list[SignalRecord]:
        return [r for r in self.records if r.proposal is not None]

    def summary(self) -> dict[str, Any]:
        return {"bar_ts": self.bar_ts.isoformat(), "regime": self.regime.to_json(),
                "evaluated": self.evaluated,
                "proposals": [{"symbol": r.symbol, "strategy": r.strategy, "score": r.score}
                              for r in self.proposals]}


def _state(symbol: str, bars: Mapping[str, Sequence[Bar]], contexts: Mapping[str, SymbolContext],
           session: SessionDay, bar_ts: datetime) -> SymbolState | None:
    upto = [b for b in bars.get(symbol, ()) if b.ts <= bar_ts]  # no look-ahead
    return compute_features(upto, contexts.get(symbol, SymbolContext(symbol)), session)


def run_cycle(bar_ts: datetime, bars: Mapping[str, Sequence[Bar]],
              contexts: Mapping[str, SymbolContext], session: SessionDay,
              universe: Sequence[str], cfg: TradingConfig, config_version: int, *,
              scorer: Scorer | None = None,
              strategies: Sequence[Strategy] = ALL_STRATEGIES,
              news_scores: Mapping[str, float] | None = None) -> CycleResult:
    scorer = scorer or WeightedRuleScorer()
    ref = {s: _state(s, bars, contexts, session, bar_ts) for s in REFERENCE}
    # Only symbols that printed a bar this minute are evaluated (and count for breadth),
    # so skip computing features for the rest.
    active = [s for s in universe if any(b.ts == bar_ts for b in reversed(bars.get(s, ())))]
    states = {s: _state(s, bars, contexts, session, bar_ts) for s in active}
    universe_feats = [st.features for st in states.values() if st is not None]
    regime = detect({s: (st.features if st else None) for s, st in ref.items()}, universe_feats)
    result = CycleResult(bar_ts, regime)

    for symbol in sorted(universe):
        state = states.get(symbol)
        # Only symbols that printed a bar this minute: never act on a stale last bar.
        if state is None or state.features.bar_ts != bar_ts.isoformat():
            continue
        result.evaluated.append(symbol)
        best: SignalRecord | None = None
        for strat in strategies:
            rec = _evaluate(strat, state, regime, cfg, config_version, scorer, bar_ts,
                            (news_scores or {}).get(symbol, 0.5))
            result.records.append(rec)
            if rec.proposal is not None and (best is None or rec.score > best.score):
                if best is not None:
                    _supersede(best, rec.strategy)
                best = rec
            elif rec.proposal is not None and best is not None:
                _supersede(rec, best.strategy)
    return result


def _supersede(rec: SignalRecord, winner: str) -> None:
    rec.proposal, rec.explanation, rec.action = None, None, "NO_TRADE"
    rec.note = f"superseded by higher-scoring {winner} on the same bar"


def _evaluate(strat: Strategy, state: SymbolState, regime: Regime, cfg: TradingConfig,
              config_version: int, scorer: Scorer, bar_ts: datetime,
              news: float = 0.5) -> SignalRecord:
    f: Features = state.features
    ev = strat.evaluate(state, regime, cfg)
    rec = SignalRecord(symbol=f.symbol, strategy=strat.name, bar_ts=bar_ts,
                       features=f.to_json(), conditions=ev.conditions_json(), factor_scores={},
                       score=0.0, regime=regime.label, signals=list(ev.signals),
                       action="NO_TRADE")
    if ev.candidate is None:
        return rec
    sr = scorer.score(f, ev.candidate, regime, cfg.scoring, news)
    rec.score, rec.factor_scores = sr.score, sr.factor_scores
    if sr.score < cfg.scoring.min_trade_score:
        rec.note = (f"score {sr.score:.2f} below minimum {cfg.scoring.min_trade_score:.2f}")
        return rec
    proposal = TradeProposal.build(ev.candidate, sr, f, regime, config_version)
    rec.proposal, rec.action, rec.explanation = proposal, "BUY", entry_explanation(proposal)
    return rec


def replay_session(bars: Mapping[str, Sequence[Bar]], contexts: Mapping[str, SymbolContext],
                   session: SessionDay, universe: Sequence[str], cfg: TradingConfig,
                   config_version: int) -> list[CycleResult]:
    """Run every minute of a stored session in order (offline; same code as live)."""
    minutes = sorted({b.ts for s in universe for b in bars.get(s, ())
                      if session.open_at <= b.ts < session.close_at})
    return [run_cycle(m, bars, contexts, session, universe, cfg, config_version)
            for m in minutes]
