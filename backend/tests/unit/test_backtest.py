"""SimBroker fill model, exit rules, costs, metrics, and the backtest simulator."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from intraday.backtest.costs import CostModel
from intraday.backtest.metrics import (
    TradeResult,
    max_drawdown,
    sharpe,
    sortino,
    streaks,
    summarize,
)
from intraday.backtest.runner import SessionData, Simulator
from intraday.core.assets import Eligibility, RuleResult
from intraday.core.runtime_config import ExitSettings, TradingConfig
from intraday.features.feature_engineering import compute_features
from intraday.features.regime import Regime
from intraday.marketdata.types import Bar
from intraday.trading.broker.sim import check_bracket, fill_entry, fill_market_exit
from intraday.trading.exits import bar_close_exit, trailing_stop
from tests.session_fixtures import (
    SESSION,
    bars,
    breakout_day,
    breakout_volumes,
    context,
    market_up,
)

D = Decimal
T0 = SESSION.open_at
ELIG = {"AAA": Eligibility({f"E{i}": RuleResult(True, "ok") for i in range(1, 8)})}
BULL = Regime("BULLISH", "BULLISH", False, None, 0.6)


def bar(o: str, h: str, low: str, c: str, minute: int = 0) -> Bar:
    return Bar("AAA", "iex", T0 + timedelta(minutes=minute), D(o), D(h), D(low), D(c), 1000)


# ------------------------------------------------------------------ fills

def test_entry_fill_rules() -> None:
    assert fill_entry(bar("101", "102", "100.6", "101.5"), D("100.5"), 5) is None  # never reached
    f = fill_entry(bar("100", "101", "99.8", "100.5"), D("100.5"), 5)
    assert f is not None and f.price == D("100.0500")                             # open + 5 bps
    capped = fill_entry(bar("100.48", "101", "100.4", "100.9"), D("100.5"), 5)
    assert capped is not None and capped.price == D("100.5000")                   # never > limit


def test_bracket_rules() -> None:
    gap = check_bracket(bar("97", "98", "96", "97.5"), D("98"), D("104"), 10)
    assert gap is not None and gap.reason == "STOP_LOSS" and gap.price == D("96.9030")
    both = check_bracket(bar("100", "105", "97", "103"), D("98"), D("104"), 0)
    assert both is not None and both.reason == "STOP_LOSS"                        # stop first
    assert check_bracket(bar("100", "105", "99", "103"), D("98"), D("104"), 0,
                         entry_bar=True) is None                                   # entry bar
    tp = check_bracket(bar("100", "105", "99", "103"), D("98"), D("104"), 0)
    assert tp is not None and tp.reason == "TAKE_PROFIT" and tp.price == D("104")
    gap_up = check_bracket(bar("106", "107", "105.5", "106"), D("98"), D("104"), 0)
    assert gap_up is not None and gap_up.price == D("106")
    assert fill_market_exit(bar("100", "101", "99", "100"), 10) == D("99.9000")


# ------------------------------------------------------------------ exits

def test_trailing_stop_activates_and_never_lowers() -> None:
    cfg = ExitSettings()
    stop, active = trailing_stop(entry=D("100"), initial_stop=D("99"), current_stop=D("99"),
                                 high_water=D("100.9"), cfg=cfg)
    assert (stop, active) == (D("99"), False)                                      # < +1R
    stop, active = trailing_stop(entry=D("100"), initial_stop=D("99"), current_stop=D("99"),
                                 high_water=D("102"), cfg=cfg)
    assert active and stop == D("101.24")                                          # 102 x 0.9925
    stop2, _ = trailing_stop(entry=D("100"), initial_stop=D("99"), current_stop=D("101.5"),
                             high_water=D("102"), cfg=cfg)
    assert stop2 == D("101.5")                                                     # never lowers
    off = cfg.model_copy(update={"trailing_enabled": False})
    assert trailing_stop(entry=D("100"), initial_stop=D("99"), current_stop=D("99"),
                         high_water=D("110"), cfg=off) == (D("99"), False)


def test_bar_close_exit_rules() -> None:
    f = compute_features(bars("AAA", breakout_day(), breakout_volumes()), context("AAA"),
                         SESSION).features  # type: ignore[union-attr]
    now, cfg = T0 + timedelta(minutes=60), ExitSettings()
    fc = T0 + timedelta(hours=6, minutes=25)

    def run(**kw):  # type: ignore[no-untyped-def]
        args = {"f": f, "entry_time": now - timedelta(minutes=10), "now": now, "cfg": cfg,
                "regime": BULL, "closes_below_vwap": 0, "force_close": fc, **kw}
        return bar_close_exit(**args)

    assert run() is None
    assert run(now=fc).reason == "EOD_LIQUIDATION"
    assert run(entry_time=now - timedelta(minutes=91)).reason == "TIME_EXIT"
    assert run(closes_below_vwap=2).reason == "VWAP_LOSS"
    assert run(regime=Regime("BEARISH", "BEARISH", False, None, 0.2)).reason == "REGIME_REVERSAL"
    rev = replace(f, ema9_prev=2.0, ema20_prev=1.0, ema9=1.0, ema20=2.0, macd_hist=-0.1)
    assert run(f=rev).reason == "MOMENTUM_REVERSAL"


# ------------------------------------------------------------------ costs & metrics

def test_costs() -> None:
    c = CostModel()
    assert c.buy_fees(100, D("50")) == D("0.00")
    assert c.sell_fees(100, D("50")) == D("0.16")      # SEC 0.139 + TAF 0.0166
    assert c.sell_fees(100_000, D("1")) == D("11.08")  # TAF capped at 8.30


def test_metric_primitives() -> None:
    assert max_drawdown([100, 120, 90, 130, 100]) == (30, pytest.approx(0.25))
    assert sharpe([0.01, 0.01]) is None
    s = sharpe([0.01, -0.005, 0.02, 0.0])
    assert s is not None and s > 0
    assert sortino([0.01, 0.02]) is None
    assert streaks([D(1), D(2), D(-1), D(-1), D(-1), D(3)]) == (2, 3)


def trade(net: str, minutes: int = 30, strat: str = "vwap_reclaim") -> TradeResult:
    t = datetime(2026, 9, 21, 14, 0)
    return TradeResult("AAA", strat, date(2026, 9, 21), t, t + timedelta(minutes=minutes), 10,
                       D("100"), D("101"), D(net), D("0.1"), D(net), "TAKE_PROFIT", 0.8)


def test_summarize() -> None:
    trades = [trade("100"), trade("-50"), trade("30", strat="pullback")]
    m = summarize(trades, [(date(2026, 9, 21), 100_080.0)], 100_000.0)
    assert m["trades"] == 3 and m["win_rate_pct"] == pytest.approx(66.67)
    assert m["profit_factor"] == pytest.approx(2.6)
    assert m["average_win"] == 65.0 and m["average_loss"] == -50.0
    assert m["largest_loss"] == -50.0 and m["total_return_pct"] == pytest.approx(0.08)
    assert m["by_strategy"]["pullback"]["trades"] == 1
    assert m["exit_reasons"] == {"TAKE_PROFIT": 3}
    empty = summarize([], [], 100_000.0)
    assert empty["trades"] == 0 and empty["win_rate_pct"] is None


# ------------------------------------------------------------------ simulator

def session(closes: list[float], vols: list[float]) -> SessionData:
    b = {"AAA": bars("AAA", closes, vols), **market_up(len(closes))}
    return SessionData(SESSION, b, {s: context(s, warm_close=float(v[0].close))
                                    for s, v in b.items()})


def sim(cfg: TradingConfig | None = None, elig: dict | None = None) -> Simulator:  # type: ignore[type-arg]
    return Simulator(cfg or TradingConfig(), CostModel(), D("100000"), feed="iex",
                     eligibility=ELIG if elig is None else elig, sectors={"AAA": "Tech"})


def breakout_then(tail: list[float]) -> SessionData:
    closes = breakout_day() + tail
    return session(closes, breakout_volumes() + [1500.0] * len(tail))


def test_entries_execute_on_the_next_bar() -> None:
    s = sim()
    s.run_session(breakout_then([101.6 + 0.01 * i for i in range(10)]), ["AAA"])
    orders = [e for e in s.events if e.type == "ENTRY_ORDER"]
    entries = [e for e in s.events if e.type == "ENTRY"]
    assert orders and entries
    # Decided at a bar's close (= next bar start) and filled on that next bar.
    assert entries[0].ts == orders[0].ts


def test_take_profit_exit() -> None:
    s = sim()
    s.run_session(breakout_then([101.8, 102.5, 103.4, 104.5, 104.6]), ["AAA"])
    assert [t.exit_reason for t in s.trades] == ["TAKE_PROFIT"]
    assert s.trades[0].net_pnl > 0


def test_stop_loss_exit() -> None:
    s = sim()
    s.run_session(breakout_then([101.4, 100.8, 99.9, 99.5, 99.4]), ["AAA"])
    assert s.trades and s.trades[0].exit_reason == "STOP_LOSS"
    assert s.trades[0].net_pnl < 0


def test_no_look_ahead_in_simulation() -> None:
    base = breakout_then([101.8, 102.5, 103.4, 104.5, 104.6])
    other = breakout_then([90.0, 85.0, 80.0, 75.0, 70.0])
    a, b = sim(), sim()
    a.run_session(base, ["AAA"])
    b.run_session(other, ["AAA"])
    cutoff = T0 + timedelta(minutes=len(breakout_day()))
    before = lambda s: [(e.ts, e.type, e.detail) for e in s.events if e.ts < cutoff]  # noqa: E731
    assert before(a) == before(b)


def test_force_close_liquidates() -> None:
    long_day = breakout_day() + [101.6 + 0.001 * i for i in range(390 - len(breakout_day()))]
    s = sim()
    s.run_session(session(long_day, breakout_volumes() + [1500.0] * (390 - 43)), ["AAA"])
    assert s.trades
    last = s.trades[-1]
    assert last.exit_reason in {"EOD_LIQUIDATION", "TRAILING_STOP", "TIME_EXIT", "VWAP_LOSS",
                                "TAKE_PROFIT", "STOP_LOSS", "MOMENTUM_REVERSAL"}
    assert all(t.exit_time <= SESSION.close_at for t in s.trades)
    assert not s.positions                          # nothing carried overnight


def test_daily_loss_latch_blocks_further_entries() -> None:
    cfg = TradingConfig()
    tiny = cfg.model_copy(update={"risk": cfg.risk.model_copy(update={
        "max_daily_loss": D("20"), "max_loss_per_trade": D("20"),
        "max_trades_per_symbol_per_day": 10, "reentry_cooldown_minutes": 0})})
    s = sim(tiny)
    s.run_session(breakout_then([101.4, 100.8, 99.9, 99.5, 99.4, *breakout_day()[-5:]]), ["AAA"])
    latch = next(e for e in s.events if e.type == "DAILY_LOSS_LIMIT")
    assert [t.exit_reason for t in s.trades] == ["STOP_LOSS"]
    assert s.trades[0].net_pnl <= -20            # the stop-out used the whole budget
    assert not [e for e in s.events if e.type == "ENTRY" and e.ts > latch.ts]
    # While the first position was open, R18 (worst-case daily loss) blocked a second one.
    assert any(e.type == "RISK_REJECTED" and "R18" in e.detail for e in s.events)


def test_ineligible_symbols_never_trade() -> None:
    blocked = {"AAA": Eligibility({"E7": RuleResult(False, "ETF")})}
    s = sim(elig=blocked)
    s.run_session(breakout_then([101.8, 102.5]), ["AAA"])
    assert not s.trades
    assert any(e.type == "RISK_REJECTED" and "R26" in e.detail for e in s.events)
