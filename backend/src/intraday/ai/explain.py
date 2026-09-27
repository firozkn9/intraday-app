"""Template explanations from recorded decision data (SPEC §23; docs/design/09 §8).

Deterministic: the same proposal always renders the same text, and every
number comes from the proposal/features, so the explanation can't contradict
them. In EXPLAIN/VETO mode (Phase 9) an LLM may rephrase this text, guarded by
a number check.
"""

from __future__ import annotations

from intraday.ai.decision import TradeProposal

STRATEGY_LABELS = {"momentum_breakout": "Momentum Breakout", "vwap_reclaim": "VWAP Reclaim",
                   "pullback": "Pullback"}

EXIT_TEMPLATES = {
    "TAKE_PROFIT": "Take-profit reached at ${price}",
    "STOP_LOSS": "Stop-loss triggered at ${price}",
    "TRAILING_STOP": "Trailing stop hit at ${price}",
    "TIME_EXIT": "Time exit after {minutes} minutes",
    "MOMENTUM_REVERSAL": "Momentum reversal: EMA 9 crossed below EMA 20 with MACD negative",
    "VWAP_LOSS": "VWAP breakdown: {bars} closes below VWAP (${vwap})",
    "REGIME_REVERSAL": "Market regime turned {regime}",
    "EOD_LIQUIDATION": "End-of-day liquidation at ${price}",
    "EMERGENCY": "Emergency stop",
    "MANUAL": "Closed manually",
    "DAILY_LOSS_FLATTEN": "Daily loss limit reached: position flattened",
    "RECONCILE": "Closed by the broker (reconciled)",
}


def entry_explanation(p: TradeProposal) -> str:
    label = STRATEGY_LABELS.get(p.strategy, p.strategy)
    lines = [f"{p.symbol} proposed via {label} because:"]
    lines += [f"• {s}" for s in p.signals]
    regime = p.market_context.get("label", "UNKNOWN")
    lines.append(f"• Market regime {regime}")
    lines.append(f"• Risk/reward = {p.risk_reward_ratio:.1f} (stop ${p.stop_loss}, "
                 f"target ${p.take_profit})")
    lines.append(f"• Model confidence = {p.confidence:.0%} (score, not a probability of profit)")
    return "\n".join(lines)


def exit_explanation(reason: str, **values: object) -> str:
    template = EXIT_TEMPLATES.get(reason, reason.replace("_", " ").capitalize())
    try:
        return template.format(**values)
    except KeyError:
        return template.split(":")[0].replace("${price}", "").strip()
