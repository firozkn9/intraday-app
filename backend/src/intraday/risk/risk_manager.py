"""Risk engine: final authority on every entry (SPEC §2; docs/design/07).

``evaluate`` is pure and deterministic. It runs **every** check (R1-R26) and
records each result, even after one fails, so the audit trail shows everything
that was wrong. ``safe_evaluate`` turns any exception into a rejection; the
engine then latches RISK_ENGINE_ERROR (fail closed).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from intraday.core.hard_limits import HARD_MAX_STOP_LOSS_PERCENT, HARD_MIN_STOP_LOSS_PERCENT
from intraday.core.runtime_config import TradingConfig
from intraday.features.regime import entries_allowed
from intraday.risk import daily_loss, position_sizer
from intraday.risk.models import ZERO, CheckResult, RiskDecision, RiskInputs, Sizing

HUNDRED = Decimal("100")


def evaluate(inputs: RiskInputs, cfg: TradingConfig) -> RiskDecision:
    p, pf, acct, mkt = inputs.proposal, inputs.portfolio, inputs.account, inputs.market
    checks: list[CheckResult] = []
    add = checks.append
    sizing = position_sizer.size(inputs, cfg)
    lim = sizing.effective_limits
    qty = sizing.qty
    rps = sizing.risk_per_share
    value = p.entry * qty
    max_loss = rps * qty if rps > 0 else ZERO

    # R1 trading gate
    blocks = inputs.gate.blocks
    add(CheckResult("R1", "Trading gate", not blocks,
                    "gate open" if not blocks else "blocked: " + ", ".join(blocks),
                    value=list(blocks) or None))
    # R2 paper mode (re-asserted; the real guard runs at boot and in the broker factory)
    add(CheckResult("R2", "Paper mode", inputs.gate.paper_mode_ok,
                    "paper mode verified" if inputs.gate.paper_mode_ok
                    else "paper-mode guard failed"))
    # R3 trading hours
    w, now = inputs.clock.window, inputs.clock.now
    in_window = inputs.clock.market_open and w is not None and w.entries_open(now)
    add(CheckResult("R3", "Trading hours", in_window,
                    "inside the entry window" if in_window else
                    ("market closed" if not inputs.clock.market_open else
                     "outside the entry window")))
    # R4 long only, not already held
    long_ok = p.side == "BUY" and not pf.holds(p.symbol)
    add(CheckResult("R4", "Long only, no existing position", long_ok,
                    "new long entry" if long_ok else
                    ("short/sell entries are never allowed" if p.side != "BUY"
                     else f"already holding {p.symbol}")))
    # R5 minimum price
    floor = lim["min_stock_price"]
    add(CheckResult("R5", "Minimum price", mkt.last_price >= floor,
                    f"${mkt.last_price} vs ${floor}", mkt.last_price, floor))
    # R6 data quality
    add(CheckResult("R6", "Data quality", not mkt.data_problems,
                    "; ".join(mkt.data_problems) or "fresh, consistent data",
                    list(mkt.data_problems) or None))
    # R7 spread (feed-specific threshold)
    max_spread = Decimal(str(cfg.universe.max_spread_percent_sip if inputs.feed == "sip"
                             else cfg.universe.max_spread_percent_iex))
    sp = mkt.spread_pct
    add(CheckResult("R7", "Spread", sp is not None and sp <= max_spread,
                    "no fresh two-sided quote" if sp is None
                    else f"{sp.quantize(Decimal('0.0001'))}% vs {max_spread}%", sp, max_spread))
    # R8 stop-loss present and sane
    stop_pct = (p.entry - p.stop) / p.entry * HUNDRED if p.entry > 0 else ZERO
    lo, hi = Decimal(str(HARD_MIN_STOP_LOSS_PERCENT)), Decimal(str(HARD_MAX_STOP_LOSS_PERCENT))
    stop_ok = p.stop > 0 and p.stop < p.entry and lo <= stop_pct <= hi
    add(CheckResult("R8", "Stop-loss", stop_ok,
                    f"stop ${p.stop} = {stop_pct.quantize(Decimal('0.01'))}% below entry "
                    f"(allowed {lo}-{hi}%)", stop_pct, [lo, hi]))
    # R9 take-profit present
    add(CheckResult("R9", "Take-profit", p.target > p.entry,
                    f"target ${p.target} vs entry ${p.entry}", p.target))
    # R10 reward/risk
    rr = ((p.target - p.entry) / (p.entry - p.stop)) if p.entry > p.stop else None
    min_rr = Decimal(str(cfg.risk.min_risk_reward))
    add(CheckResult("R10", "Reward/risk", rr is not None and rr >= min_rr,
                    f"{rr.quantize(Decimal('0.01')) if rr is not None else 'n/a'} vs {min_rr}",
                    rr, min_rr))
    # R11 daily trade count
    add(CheckResult("R11", "Daily trades", pf.trades_today < cfg.risk.max_daily_trades,
                    f"{pf.trades_today} of {cfg.risk.max_daily_trades} used",
                    pf.trades_today, cfg.risk.max_daily_trades))
    # R12 per-symbol trade count
    n_sym = pf.symbol_trades_today.get(p.symbol, 0)
    add(CheckResult("R12", "Trades in this symbol", n_sym < cfg.risk.max_trades_per_symbol_per_day,
                    f"{n_sym} of {cfg.risk.max_trades_per_symbol_per_day} used", n_sym,
                    cfg.risk.max_trades_per_symbol_per_day))
    # R13 re-entry cooldown
    last_exit = pf.last_exit_at.get(p.symbol)
    cool = timedelta(minutes=cfg.risk.reentry_cooldown_minutes)
    cool_ok = last_exit is None or now - last_exit >= cool
    add(CheckResult("R13", "Re-entry cooldown", cool_ok,
                    "no recent exit" if last_exit is None else
                    f"last exit {int((now - last_exit).total_seconds() // 60)} min ago "
                    f"(cooldown {cfg.risk.reentry_cooldown_minutes} min)"))
    # R14 open positions (incl. pending entries)
    open_now = len(pf.positions) + len(pf.pending)
    add(CheckResult("R14", "Open positions", open_now < cfg.risk.max_open_positions,
                    f"{open_now} open/pending of {cfg.risk.max_open_positions}", open_now,
                    cfg.risk.max_open_positions))
    # R15 sized quantity
    min_val = cfg.risk.min_position_value
    add(CheckResult("R15", "Position size", qty >= 1 and value >= min_val,
                    f"{qty} shares = ${value.quantize(Decimal('0.01'))} (binding: "
                    f"{sizing.binding}; minimum ${min_val})", qty, min_val))
    # R16 total exposure (re-verified after sizing)
    exposure_after = pf.exposure + pf.pending_exposure + value
    add(CheckResult("R16", "Total exposure", exposure_after <= lim["max_total_exposure"],
                    f"${exposure_after.quantize(Decimal('0.01'))} vs "
                    f"${lim['max_total_exposure']}", exposure_after, lim["max_total_exposure"]))
    # R17 max loss per trade (incl. slippage buffer)
    add(CheckResult("R17", "Max loss per trade", max_loss <= lim["max_loss_per_trade"],
                    f"${max_loss.quantize(Decimal('0.01'))} vs ${lim['max_loss_per_trade']}",
                    max_loss, lim["max_loss_per_trade"]))
    # R18 daily loss, worst case
    status = daily_loss.status(pf, lim["max_daily_loss"])
    room = daily_loss.headroom(pf, lim["max_daily_loss"], cfg.risk.daily_loss_basis)
    worst_after = status.loss_used + (pf.open_risk if cfg.risk.daily_loss_basis ==
                                      "WORST_CASE_OPEN_RISK" else ZERO) + max_loss
    add(CheckResult("R18", "Daily loss (worst case)",
                    not status.breached and max_loss <= room,
                    f"worst case ${worst_after.quantize(Decimal('0.01'))} vs "
                    f"${lim['max_daily_loss']} ({cfg.risk.daily_loss_basis})",
                    worst_after, lim["max_daily_loss"]))
    # R19 buying power (broker truth when available)
    bp = acct.buying_power * Decimal(str(cfg.risk.buying_power_utilization))
    add(CheckResult("R19", "Buying power", value <= bp,
                    f"${value.quantize(Decimal('0.01'))} vs ${bp.quantize(Decimal('0.01'))} "
                    f"({acct.source.lower()})", value, bp))
    # R20 sector exposure
    sector_after = pf.sector_exposure(mkt.sector, p.symbol) + value
    add(CheckResult("R20", "Sector exposure", sector_after <= lim["max_sector_exposure"],
                    f"{mkt.sector or 'unclassified'}: ${sector_after.quantize(Decimal('0.01'))} "
                    f"vs ${lim['max_sector_exposure']}", sector_after,
                    lim["max_sector_exposure"]))
    # R21 correlated exposure
    corr_after = inputs.correlated_exposure + value
    add(CheckResult("R21", "Correlated exposure", corr_after <= lim["max_correlated_exposure"],
                    f"cluster ${corr_after.quantize(Decimal('0.01'))} vs "
                    f"${lim['max_correlated_exposure']}", corr_after,
                    lim["max_correlated_exposure"]))
    # R22 regime policy
    ok, detail = entries_allowed(inputs.regime, cfg.regime)
    if ok and sizing.multiplier != 1:
        detail += f" (size x {sizing.multiplier})"
    add(CheckResult("R22", "Market regime", ok, detail, inputs.regime.label))
    # R23 news events
    blocked = [t for t in mkt.blocked_news_tags if t in cfg.news.block_event_tags]
    add(CheckResult("R23", "News events", not blocked,
                    "no blocking news events" if not blocked
                    else "blocked by: " + ", ".join(blocked), blocked or None))
    # R24 duplicate intent
    dup = pf.pending_for(p.symbol)
    add(CheckResult("R24", "No duplicate entry", not dup,
                    "no pending entry for the symbol" if not dup
                    else f"an entry for {p.symbol} is already pending"))
    # R25 LLM veto consistency: the LLM can only remove trades
    veto_ok = p.ai_mode != "VETO" or p.llm_verdict == "APPROVE"
    add(CheckResult("R25", "AI veto", veto_ok,
                    "not applicable (AI mode " + p.ai_mode + ")" if p.ai_mode != "VETO"
                    else f"LLM verdict {p.llm_verdict or 'missing'}"))
    # R26 asset eligibility, re-checked independently of the scanner (CLAUDE.md)
    elig = mkt.eligibility
    add(CheckResult("R26", "Asset eligibility", elig.eligible,
                    "U.S. common stock on NYSE/Nasdaq" if elig.eligible
                    else "fails " + ", ".join(elig.failed), elig.failed or None))

    approved = all(c.passed for c in checks) and qty >= 1
    return RiskDecision(approved=approved, checks=tuple(checks), sizing=sizing,
                        qty=qty if approved else 0, position_value=value, max_loss=max_loss,
                        exposure_after=exposure_after, open_positions_after=open_now + 1,
                        daily_loss_worst_case_after=worst_after, risk_reward=rr)


def safe_evaluate(inputs: RiskInputs, cfg: TradingConfig) -> RiskDecision:
    """Any exception → rejection (fail closed); the caller latches RISK_ENGINE_ERROR."""
    try:
        return evaluate(inputs, cfg)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:500]
        empty = Sizing(0, {}, None, ZERO, Decimal("1"), {})
        return RiskDecision(False, (CheckResult("ERR", "Risk engine error", False, err),),
                            empty, 0, ZERO, ZERO, ZERO, 0, ZERO, None, error=err)
