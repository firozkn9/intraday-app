"""Strategies A/B/C, stops, scoring, regime, proposals, explanations, and the pipeline."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from intraday.ai.decision import TradeProposal
from intraday.ai.explain import entry_explanation, exit_explanation
from intraday.ai.pipeline import replay_session, run_cycle
from intraday.ai.scoring import REGIME_SCORE, WeightedRuleScorer, volume_score
from intraday.core.runtime_config import TradingConfig
from intraday.features.feature_engineering import compute_features
from intraday.features.regime import Regime, detect, entries_allowed
from intraday.strategies import MomentumBreakout
from intraday.strategies.base import place_stop
from tests.session_fixtures import (
    SESSION,
    bars,
    breakout_day,
    breakout_volumes,
    context,
    market_down,
    market_up,
    pullback_day,
    pullback_volumes,
    reclaim_day,
)

CFG = TradingConfig()
BULL = Regime("BULLISH", "BULLISH", False, None, 1.0)
BEAR = Regime("BEARISH", "BEARISH", False, None, 0.2)


def cycle(symbol: str, closes: list[float], vols: list[float], market=None, cfg=CFG):  # type: ignore[no-untyped-def]
    market = market if market is not None else market_up(len(closes))
    b = {symbol: bars(symbol, closes, vols), **market}
    ctx = {s: context(s, warm_close=float(b[s][0].close)) for s in b}
    return run_cycle(b[symbol][-1].ts, b, ctx, SESSION, [symbol], cfg, 7), b, ctx


def rec(result, strategy: str):  # type: ignore[no-untyped-def]
    return next(r for r in result.records if r.strategy == strategy)


# ------------------------------------------------------------------ strategies

def test_momentum_breakout_proposal() -> None:
    r, _, _ = cycle("AAA", breakout_day(), breakout_volumes())
    mb = rec(r, "momentum_breakout")
    assert mb.action == "BUY" and mb.proposal is not None
    assert all(c["passed"] for c in mb.conditions.values())
    p = mb.proposal
    assert p.entry_ref_price == Decimal("101.50")
    assert p.stop_loss == Decimal("100.49") and p.take_profit == Decimal("103.53")  # 1% / 2%
    assert p.risk_reward_ratio == pytest.approx(2.0, abs=0.02)
    assert p.confidence == p.score and p.config_version == 7
    assert any("Breakout above 15-bar high" in s for s in p.signals)


def test_vwap_reclaim_proposal() -> None:
    r, _, _ = cycle("BBB", reclaim_day(), [1000.0] * 34 + [2500.0])
    vr = rec(r, "vwap_reclaim")
    assert vr.action == "BUY"
    assert vr.conditions["was_below_vwap"]["value"] >= 5
    assert vr.proposal is not None and "reclaimed VWAP" in vr.proposal.signals[0]


def test_pullback_conditions_pass_but_score_gate_applies() -> None:
    r, _, _ = cycle("CCC", pullback_day(), pullback_volumes())
    pb = rec(r, "pullback")
    assert all(c["passed"] for c in pb.conditions.values())
    assert pb.action == "NO_TRADE" and "below minimum 0.70" in (pb.note or "")
    lenient = CFG.model_copy(update={"scoring": CFG.scoring.model_copy(
        update={"min_trade_score": 0.6})})
    r2, _, _ = cycle("CCC", pullback_day(), pullback_volumes(), cfg=lenient)
    assert rec(r2, "pullback").action == "BUY"


def test_every_condition_is_recorded_on_failure() -> None:
    r, _, _ = cycle("CCC", pullback_day(), pullback_volumes())
    mb = rec(r, "momentum_breakout")
    assert set(mb.conditions) == {"above_vwap", "ema9_gt_ema20", "breakout", "rvol",
                                  "volume_rising", "regime_allows"}
    assert not mb.conditions["breakout"]["passed"]
    assert mb.proposal is None and mb.score == 0.0


def test_bearish_regime_blocks_longs() -> None:
    b = bars("AAA", breakout_day(), breakout_volumes())
    state = compute_features(b, context("AAA"), SESSION)
    assert state is not None
    ev = MomentumBreakout().evaluate(state, BEAR, CFG)
    assert not ev.passed and ev.conditions_json()["regime_allows"]["passed"] is False
    reduce = CFG.model_copy(update={"regime": CFG.regime.model_copy(
        update={"bearish_policy": "REDUCE"})})
    assert MomentumBreakout().evaluate(state, BEAR, reduce).passed


def test_disabled_strategy_never_proposes() -> None:
    off = CFG.model_copy(update={"strategies": CFG.strategies.model_copy(update={
        "momentum_breakout": CFG.strategies.momentum_breakout.model_copy(
            update={"enabled": False})})})
    r, _, _ = cycle("AAA", breakout_day(), breakout_volumes(), cfg=off)
    mb = rec(r, "momentum_breakout")
    assert mb.action == "NO_TRADE" and mb.conditions["enabled"]["passed"] is False


def test_best_strategy_per_symbol_wins_others_superseded() -> None:
    r, _, _ = cycle("AAA", breakout_day(), breakout_volumes())
    assert [p.strategy for p in r.proposals] == ["momentum_breakout"]
    pb = rec(r, "pullback")
    assert pb.score > 0 and "superseded by higher-scoring momentum_breakout" in (pb.note or "")


# ------------------------------------------------------------------ stops

@pytest.mark.parametrize(("method", "structure", "atr", "expected_pct", "used"), [
    ("PERCENT", None, None, 1.0, "PERCENT"),
    ("ATR", None, 0.4, 0.6, "ATR"),            # 1.5 x 0.4 / 100
    ("ATR", None, 3.0, 2.0, "ATR"),            # 4.5% clamped to max_stop_percent 2%
    ("STRUCTURE", 99.5, None, 0.5497, "STRUCTURE"),
    ("STRUCTURE", 99.95, None, 0.2, "STRUCTURE"),  # too tight → hard minimum 0.2%
    ("STRUCTURE", 101.0, None, 1.0, "PERCENT"),    # structure above entry → fallback
])
def test_stop_placement(method: str, structure: float | None, atr: float | None,
                        expected_pct: float, used: str) -> None:
    stop, got, _ = place_stop(100.0, method, CFG, atr=atr, atr_multiple=1.5, max_stop_pct=2.0,
                              structure=structure)
    assert got == used
    assert (100 - stop) == pytest.approx(expected_pct, abs=1e-3)


# ------------------------------------------------------------------ scoring

def test_scores_are_bounded_and_weighted() -> None:
    r, _, _ = cycle("AAA", breakout_day(), breakout_volumes())
    mb = rec(r, "momentum_breakout")
    w = CFG.scoring.weights
    assert mb.score == pytest.approx(sum(w[k] * v for k, v in mb.factor_scores.items()), abs=1e-3)
    assert all(0 <= v <= 1 for v in mb.factor_scores.values())
    assert mb.factor_scores["news"] == 0.5  # UNKNOWN news is neutral


def test_volume_score_is_monotone_in_rvol() -> None:
    b = bars("AAA", breakout_day(), breakout_volumes())
    f = compute_features(b, context("AAA"), SESSION).features  # type: ignore[union-attr]
    scores = [volume_score(replace(f, rvol=x)) for x in (0.5, 1.0, 1.5, 2.0, 3.0, 5.0)]
    assert scores == sorted(scores)


def test_regime_factor_mapping() -> None:
    assert REGIME_SCORE["BULLISH"] > REGIME_SCORE["SIDEWAYS"] > REGIME_SCORE["HIGH_VOLATILITY"]
    assert REGIME_SCORE["BEARISH"] == REGIME_SCORE["UNKNOWN"] == 0.0


def test_scorer_respects_custom_weights() -> None:
    b = bars("AAA", breakout_day(), breakout_volumes())
    state = compute_features(b, context("AAA"), SESSION)
    cand = MomentumBreakout().evaluate(state, BULL, CFG).candidate  # type: ignore[arg-type]
    only_regime = CFG.scoring.model_copy(update={
        "weight_momentum": 0.0, "weight_volume": 0.0, "weight_price_action": 0.0,
        "weight_market_regime": 1.0, "weight_volatility": 0.0, "weight_technical": 0.0,
        "weight_news": 0.0})
    assert WeightedRuleScorer().score(state.features, cand, BULL, only_regime).score == 1.0  # type: ignore[union-attr, arg-type]


# ------------------------------------------------------------------ regime

def test_regime_detection() -> None:
    up = market_up(40)
    feats = {s: compute_features(b, context(s, warm_close=float(b[0].close)), SESSION)
             for s, b in up.items()}
    ref = {s: st.features for s, st in feats.items() if st}
    assert detect(ref, []).trend == "BULLISH"
    assert detect({"SPY": ref["SPY"], "QQQ": None}, []).label == "UNKNOWN"

    down = market_down(40)
    dref = {s: compute_features(b, context(s, warm_close=float(b[0].close)),
                                SESSION).features for s, b in down.items()}  # type: ignore[union-attr]
    below = [replace(ref["SPY"], price=1.0, vwap=2.0)]  # breadth 0%
    assert detect(dref, below).trend == "BEARISH"

    early = {s: replace(f, minute_index=2) for s, f in ref.items()}
    assert "warming up" in detect(early, []).reasons[0]

    hot = {"SPY": replace(ref["SPY"], rv_percentile=95.0), "QQQ": ref["QQQ"]}
    r = detect(hot, [])
    assert r.label == "HIGH_VOLATILITY" and r.high_volatility and r.trend == "BULLISH"


def test_regime_policy() -> None:
    unknown = Regime("UNKNOWN", "UNKNOWN", False, None, None)
    assert entries_allowed(unknown, CFG.regime)[0] is False
    assert entries_allowed(BEAR, CFG.regime)[0] is False
    assert entries_allowed(BULL, CFG.regime)[0] is True
    reduce = CFG.regime.model_copy(update={"unknown_policy": "REDUCE"})
    assert entries_allowed(unknown, reduce)[0] is True


# ------------------------------------------------------------------ pipeline

def test_no_look_ahead_in_pipeline() -> None:
    closes, vols = breakout_day(), breakout_volumes()
    full = {"AAA": bars("AAA", closes, vols), **market_up(len(closes))}
    ctx = {s: context(s, warm_close=float(b[0].close)) for s, b in full.items()}
    t = full["AAA"][30].ts
    changed = {s: [b if b.ts <= t else replace(b, close=b.close * 2, high=b.high * 2)
                   for b in bs] for s, bs in full.items()}
    a = run_cycle(t, full, ctx, SESSION, ["AAA"], CFG, 1)
    b = run_cycle(t, changed, ctx, SESSION, ["AAA"], CFG, 1)
    assert [x.features for x in a.records] == [x.features for x in b.records]
    assert [x.score for x in a.records] == [x.score for x in b.records]


def test_symbols_without_a_bar_this_minute_are_skipped() -> None:
    closes, vols = breakout_day(), breakout_volumes()
    b = {"AAA": bars("AAA", closes, vols)[:-1], **market_up(len(closes))}
    ctx = {s: context(s, warm_close=float(v[0].close)) for s, v in b.items()}
    r = run_cycle(b["SPY"][-1].ts, b, ctx, SESSION, ["AAA"], CFG, 1)
    assert r.evaluated == [] and r.records == []


def test_replay_session_emits_cycles_and_proposals() -> None:
    closes, vols = breakout_day(), breakout_volumes()
    b = {"AAA": bars("AAA", closes, vols), **market_up(len(closes))}
    ctx = {s: context(s, warm_close=float(v[0].close)) for s, v in b.items()}
    cycles = replay_session(b, ctx, SESSION, ["AAA"], CFG, 1)
    assert len(cycles) == len(closes)
    assert cycles[-1].proposals and cycles[-1].bar_ts == b["AAA"][-1].ts
    assert all(c.bar_ts < cycles[-1].bar_ts for c in cycles[:-1])


# ------------------------------------------------------------------ explanations

def test_entry_explanation_uses_recorded_numbers_only() -> None:
    r, _, _ = cycle("AAA", breakout_day(), breakout_volumes())
    p: TradeProposal = r.proposals[0].proposal  # type: ignore[assignment]
    text = entry_explanation(p)
    assert text.startswith("AAA proposed via Momentum Breakout because:")
    assert f"stop ${p.stop_loss}, target ${p.take_profit}" in text
    assert f"Model confidence = {p.confidence:.0%}" in text
    assert "not a probability of profit" in text
    assert entry_explanation(p) == text  # deterministic


def test_exit_explanations() -> None:
    assert exit_explanation("TAKE_PROFIT", price="204.12") == "Take-profit reached at $204.12"
    assert "2 closes below VWAP" in exit_explanation("VWAP_LOSS", bars=2, vwap="201.05")
    assert exit_explanation("EMERGENCY") == "Emergency stop"


def test_minute_timestamps_align_with_session() -> None:
    b = bars("AAA", [100.0, 100.1])
    assert b[0].ts == SESSION.open_at and b[1].ts == SESSION.open_at + timedelta(minutes=1)
