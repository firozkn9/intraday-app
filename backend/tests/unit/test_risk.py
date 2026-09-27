"""Risk engine (docs/design/07): sizing, every check R1-R26, daily loss, properties."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from intraday.core.assets import Eligibility, RuleResult
from intraday.core.runtime_config import TradingConfig
from intraday.features.regime import Regime
from intraday.marketdata.calendar import SessionDay, entry_window
from intraday.risk import daily_loss
from intraday.risk.correlation import CorrelationModel, build, pearson
from intraday.risk.models import (
    AccountSnapshot,
    Clock,
    GateSnapshot,
    MarketSnapshot,
    OpenPosition,
    PendingEntry,
    PortfolioSnapshot,
    RiskInputs,
    RiskProposal,
)
from intraday.risk.position_sizer import regime_multiplier
from intraday.risk.risk_manager import evaluate, safe_evaluate

D = Decimal
SESSION = SessionDay.from_alpaca({"date": "2026-09-21", "open": "09:30", "close": "16:00"})
NOW = SESSION.open_at + timedelta(hours=1, minutes=30)   # 11:00 ET
CFG = TradingConfig()
ELIGIBLE = Eligibility({f"E{i}": RuleResult(True, "ok") for i in range(1, 8)})
BULL = Regime("BULLISH", "BULLISH", False, None, 0.6)


def cfg_with(**risk: Any) -> TradingConfig:
    return CFG.model_copy(update={"risk": CFG.risk.model_copy(update=risk)})


def base(**over: Any) -> RiskInputs:
    inputs = RiskInputs(
        proposal=RiskProposal("AAPL", "BUY", D("50.00"), D("49.50"), D("51.00")),
        portfolio=PortfolioSnapshot(),
        account=AccountSnapshot(D("100000"), D("100000"), D("100000")),
        market=MarketSnapshot("AAPL", D("50.00"), D("0.05"), (), ELIGIBLE, "Technology"),
        gate=GateSnapshot((), True),
        clock=Clock(NOW, True, entry_window(SESSION, CFG.hours)),
        regime=BULL, feed="iex")
    return replace(inputs, **over)


def check(decision, cid: str):  # type: ignore[no-untyped-def]
    return next(c for c in decision.checks if c.id == cid)


# ------------------------------------------------------------------ sizing

def test_spec_section_5_example() -> None:
    """$100k account, $2k per trade, $100 max loss, $50 stock, 1% stop → 40 shares."""
    cfg = cfg_with(max_capital_per_trade=D("2000"), slippage_buffer_bps=0)
    d = evaluate(base(), cfg)
    assert d.sizing.bounds["risk"] == 200 and d.sizing.bounds["capital"] == 40
    assert d.sizing.binding == "capital"
    assert d.approved and d.qty == 40
    assert d.position_value == D("2000.00") and d.max_loss == D("20.00")


def test_default_config_case_with_slippage_buffer() -> None:
    d = evaluate(base(), CFG)  # $10k per trade, 5 bps buffer
    assert d.sizing.risk_per_share == D("0.525")
    assert d.sizing.bounds["capital"] == 200 and d.sizing.bounds["risk"] == 190
    assert d.approved and d.qty == 190 and d.max_loss == D("99.750")
    assert len(d.checks) == 26 and d.failed == []


def test_effective_limits_follow_equity() -> None:
    """Hard caps (1% loss/trade, 25% capital/trade) bind when equity is small."""
    small = base(account=AccountSnapshot(D("5000"), D("5000"), D("5000")))
    d = evaluate(small, CFG)
    assert d.sizing.effective_limits["max_loss_per_trade"] == D("50.00")
    assert d.sizing.effective_limits["max_capital_per_trade"] == D("1250.00")
    assert d.qty == 25  # 1250 / 50


def test_regime_multipliers() -> None:
    hot = replace(BULL, high_volatility=True, label="HIGH_VOLATILITY")
    assert regime_multiplier(hot, CFG) == D("0.5")
    reduce = CFG.model_copy(update={"regime": CFG.regime.model_copy(
        update={"unknown_policy": "REDUCE"})})
    unknown = Regime("UNKNOWN", "UNKNOWN", True, None, None)
    assert regime_multiplier(unknown, reduce) == D("0.25")
    d = evaluate(base(regime=hot), CFG)
    assert d.qty == 95 and "size x 0.5" in check(d, "R22").detail


# ------------------------------------------------------------------ every check

HELD = OpenPosition("AAPL", 10, D("49"), D("50"), D("48.5"), "Technology")


@pytest.mark.parametrize(("cid", "over", "cfg"), [
    ("R1", {"gate": GateSnapshot(("PAUSED_BY_USER",), True)}, CFG),
    ("R2", {"gate": GateSnapshot((), False)}, CFG),
    ("R3", {"clock": Clock(SESSION.open_at + timedelta(minutes=2), True,
                           entry_window(SESSION, CFG.hours))}, CFG),
    ("R3", {"clock": Clock(NOW, False, None)}, CFG),
    ("R4", {"proposal": RiskProposal("AAPL", "SELL", D("50"), D("49.5"), D("51"))}, CFG),
    ("R4", {"portfolio": PortfolioSnapshot(positions=(HELD,))}, CFG),
    ("R5", {"market": MarketSnapshot("AAPL", D("9.99"), D("0.05"), (), ELIGIBLE, "Tech")}, CFG),
    ("R6", {"market": MarketSnapshot("AAPL", D("50"), D("0.05"), ("stale bar",), ELIGIBLE,
                                     "Tech")}, CFG),
    ("R7", {"market": MarketSnapshot("AAPL", D("50"), D("0.35"), (), ELIGIBLE, "Tech")}, CFG),
    ("R7", {"market": MarketSnapshot("AAPL", D("50"), None, (), ELIGIBLE, "Tech")}, CFG),
    ("R8", {"proposal": RiskProposal("AAPL", "BUY", D("50"), D("50.10"), D("51"))}, CFG),
    ("R8", {"proposal": RiskProposal("AAPL", "BUY", D("50"), D("47"), D("56"))}, CFG),  # 6%
    ("R9", {"proposal": RiskProposal("AAPL", "BUY", D("50"), D("49.5"), D("49.9"))}, CFG),
    ("R10", {"proposal": RiskProposal("AAPL", "BUY", D("50"), D("49.5"), D("50.5"))}, CFG),
    ("R11", {"portfolio": PortfolioSnapshot(trades_today=20)}, CFG),
    ("R12", {"portfolio": PortfolioSnapshot(symbol_trades_today={"AAPL": 3})}, CFG),
    ("R13", {"portfolio": PortfolioSnapshot(last_exit_at={"AAPL": NOW - timedelta(minutes=5)})},
     CFG),
    ("R14", {"portfolio": PortfolioSnapshot(positions=tuple(
        OpenPosition(f"S{i}", 1, D("50"), D("50"), D("50"), f"Sec{i}") for i in range(5)))}, CFG),
    ("R15", {"account": AccountSnapshot(D("100000"), D("0"), D("0"))}, CFG),
    ("R18", {"portfolio": PortfolioSnapshot(realized_pnl_today=D("-500"))}, CFG),  # breached
    ("R22", {"regime": Regime("BEARISH", "BEARISH", False, None, 0.2)}, CFG),
    ("R23", {"market": MarketSnapshot("AAPL", D("50"), D("0.05"), (), ELIGIBLE, "Tech",
                                      ("earnings",))}, CFG),
    ("R24", {"portfolio": PortfolioSnapshot(pending=(
        PendingEntry("AAPL", 10, D("50"), D("49.5"), "Technology"),))}, CFG),
    ("R25", {"proposal": RiskProposal("AAPL", "BUY", D("50"), D("49.5"), D("51"), "VETO",
                                      "REJECT")}, CFG),
    ("R26", {"market": MarketSnapshot("AAPL", D("50"), D("0.05"), (), Eligibility(
        {"E1": RuleResult(True, "ok"), "E7": RuleResult(False, "name contains 'ETF'")}),
        "Tech")}, CFG),
])
def test_each_check_rejects(cid: str, over: dict[str, Any], cfg: TradingConfig) -> None:
    d = evaluate(base(**over), cfg)
    assert not d.approved and d.qty == 0
    assert cid in d.failed, (cid, d.failed)


@pytest.mark.parametrize(("over", "cfg", "binding", "qty"), [
    # Limits that sizing shrinks to fit rather than rejecting; R16-R21 re-verify afterwards.
    ({"portfolio": PortfolioSnapshot(realized_pnl_today=D("-450"))}, CFG, "daily_loss", 95),
    ({"account": AccountSnapshot(D("100000"), D("1000"), D("1000"))}, CFG, "buying_power", 18),
    ({"portfolio": PortfolioSnapshot(positions=(
        OpenPosition("MSFT", 300, D("50"), D("50"), D("50"), "Technology"),))},
     CFG, "sector", 100),
    ({"correlated_exposure": D("15000")}, CFG, "correlated", 100),
    ({"portfolio": PortfolioSnapshot(positions=(
        OpenPosition("X", 900, D("50"), D("50"), D("50"), "Energy"),))}, CFG, "exposure", 100),
])
def test_sizing_shrinks_to_fit(over: dict[str, Any], cfg: TradingConfig, binding: str,
                               qty: int) -> None:
    d = evaluate(base(**over), cfg)
    assert d.approved and d.sizing.binding == binding and d.qty == qty
    assert all(check(d, c).passed for c in ("R16", "R17", "R18", "R19", "R20", "R21"))


def test_limits_with_no_room_reject() -> None:
    full_sector = PortfolioSnapshot(positions=(
        OpenPosition("MSFT", 400, D("50"), D("50"), D("50"), "Technology"),))
    d = evaluate(base(portfolio=full_sector), CFG)
    assert not d.approved and d.sizing.bounds["sector"] == 0 and "R15" in d.failed


def test_all_checks_recorded_even_after_failures() -> None:
    d = evaluate(base(gate=GateSnapshot(("EMERGENCY_STOP",), False),
                      clock=Clock(NOW, False, None)), CFG)
    assert len(d.checks) == 26 and {"R1", "R2", "R3"} <= set(d.failed)


def test_only_gate_blocked_flag() -> None:
    d = evaluate(base(gate=GateSnapshot(("NO_BROKER",), True)), CFG)
    assert d.only_gate_blocked and d.sizing.qty == 190  # sizing still recorded


def test_llm_veto_cannot_approve_a_risk_rejection() -> None:
    p = RiskProposal("AAPL", "BUY", D("50"), D("49.5"), D("51"), "VETO", "APPROVE")
    bad_price = MarketSnapshot("AAPL", D("9"), D("0.05"), (), ELIGIBLE, "Tech")
    assert not evaluate(base(proposal=p, market=bad_price), CFG).approved


def test_risk_engine_error_fails_closed() -> None:
    broken = base(proposal=RiskProposal("AAPL", "BUY", D("NaN"), D("49.5"), D("51")))
    d = safe_evaluate(broken, CFG)
    assert not d.approved and d.error is not None and d.failed == ["ERR"]


# ------------------------------------------------------------------ daily loss

def five_positions(stop_gap: str = "0.10") -> PortfolioSnapshot:
    # Each: 100 shares, $1 of open risk to its stop → $100 each, $500 total.
    return PortfolioSnapshot(positions=tuple(
        OpenPosition(f"S{i}", 100, D("50"), D("50"), D("49"), f"Sec{i}") for i in range(4)))


def test_worst_case_open_risk_reserves_budget() -> None:
    pf = five_positions()   # 4 x $100 = $400 of open risk
    d = evaluate(base(portfolio=pf), CFG)
    assert d.sizing.bounds["daily_loss"] == 190  # (500 - 400) / 0.525
    assert d.approved and d.max_loss <= D("100")
    cur = evaluate(base(portfolio=pf), cfg_with(daily_loss_basis="CURRENT_PNL"))
    assert cur.sizing.bounds["daily_loss"] == 952  # only realized/unrealized loss counts


def test_daily_loss_status() -> None:
    pf = PortfolioSnapshot(positions=(OpenPosition("A", 100, D("50"), D("47"), D("45")),),
                           realized_pnl_today=D("-250"))
    s = daily_loss.status(pf, D("500"))
    assert s.daily_pnl == D("-550") and s.loss_used == D("550") and s.breached
    assert daily_loss.status(PortfolioSnapshot(realized_pnl_today=D("120")),
                             D("500")).loss_used == 0


def test_trailing_stop_to_breakeven_frees_budget() -> None:
    at_be = OpenPosition("A", 100, D("50"), D("51"), D("51"))
    assert at_be.open_risk == 0


# ------------------------------------------------------------------ correlation

def test_correlation_clusters() -> None:
    a = [100 + i * 0.1 + (0.05 if i % 3 else 0) for i in range(60)]
    b = [x * 1.01 for x in a]
    c = [100 + (0.2 if i % 2 else -0.2) for i in range(60)]
    model = build({"NVDA": a, "AMD": b, "XOM": c, "NEW": a[:10]}, 60,
                  {"NVDA": "Tech", "AMD": "Tech", "XOM": "Energy", "NEW": "Tech"})
    assert model.correlated("NVDA", "AMD", 0.7)
    assert not model.correlated("NVDA", "XOM", 0.7)
    assert model.correlated("NEW", "NVDA", 0.7)      # too little data → same sector counts
    assert not model.correlated("NEW", "XOM", 0.7)
    exp = model.cluster_exposure("NVDA", {"AMD": D("8000"), "XOM": D("5000")}, 0.7)
    assert exp == D("8000")
    assert pearson([1, 2, 3], [1, 1, 1]) is None
    assert CorrelationModel().cluster("X", ["Y"], 0.7) == set()


# ------------------------------------------------------------------ properties

money = st.decimals(min_value=D("5"), max_value=D("2000"), places=2)


@settings(max_examples=300, deadline=None)
@given(entry=money, stop_pct=st.decimals(min_value=D("0.2"), max_value=D("5"), places=2),
       equity=st.decimals(min_value=D("5000"), max_value=D("500000"), places=2),
       realized=st.decimals(min_value=D("-600"), max_value=D("600"), places=2),
       held=st.integers(min_value=0, max_value=6),
       corr=st.decimals(min_value=D("0"), max_value=D("25000"), places=2))
def test_approved_trades_respect_every_limit(entry: Decimal, stop_pct: Decimal, equity: Decimal,
                                             realized: Decimal, held: int, corr: Decimal) -> None:
    stop = (entry * (1 - stop_pct / 100)).quantize(D("0.01"))
    target = (entry + (entry - stop) * 2).quantize(D("0.01"))
    positions = tuple(OpenPosition(f"H{i}", 10, D("100"), D("100"), D("99"), f"S{i}")
                      for i in range(held))
    pf = PortfolioSnapshot(positions=positions, realized_pnl_today=realized)
    inputs = base(proposal=RiskProposal("AAPL", "BUY", entry, stop, target),
                  market=MarketSnapshot("AAPL", entry, D("0.05"), (), ELIGIBLE, "Technology"),
                  account=AccountSnapshot(equity, equity, equity), portfolio=pf,
                  correlated_exposure=corr)
    d = evaluate(inputs, CFG)
    if not d.approved:
        return
    lim = d.sizing.effective_limits
    assert d.qty >= 1 and isinstance(d.qty, int)
    assert d.max_loss <= lim["max_loss_per_trade"]
    assert d.position_value <= lim["max_capital_per_trade"]
    assert d.exposure_after <= lim["max_total_exposure"]
    assert d.open_positions_after <= CFG.risk.max_open_positions
    assert d.daily_loss_worst_case_after <= lim["max_daily_loss"]
    assert corr + d.position_value <= lim["max_correlated_exposure"]
