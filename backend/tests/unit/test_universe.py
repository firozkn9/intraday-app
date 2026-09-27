"""Universe filters U1-U9 (SPEC §7; docs/design/06 §3.4)."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from intraday.core.assets import Eligibility, RuleResult
from intraday.core.runtime_config import UniverseSettings
from intraday.marketdata.calendar import SessionDay
from intraday.marketdata.types import Bar, Quote
from intraday.marketdata.universe import (
    DailyVolume,
    ScanInputs,
    average_daily_volume,
    evaluate,
)

SESSION = SessionDay.from_alpaca({"date": "2026-09-21", "open": "09:30", "close": "16:00"})
OPEN = SESSION.open_at
MIDDAY = OPEN + timedelta(hours=2)          # 11:30 ET
PREMARKET = OPEN - timedelta(minutes=5)     # 09:25 ET
CFG = UniverseSettings()                    # min price 10, ADV 20k, spread 0.30% (IEX)
ELIGIBLE = Eligibility({f"E{i}": RuleResult(True, "ok") for i in range(1, 8)})


def history(n: int = 20, volume: int = 50_000, close: str = "100") -> list[DailyVolume]:
    return [DailyVolume(date(2026, 8, 1) + timedelta(days=i), volume, Decimal(close))
            for i in range(n)]


def bars_until(now: datetime, minutes: int = 30, close: str = "101",
               every: int = 1) -> list[Bar]:
    c = Decimal(close)
    return [Bar("AAPL", "iex", now - timedelta(minutes=m + 1), c, c + 1, c - 1, c, 500)
            for m in range(0, minutes, every)]


def quote(now: datetime, bid: str = "100.98", ask: str = "101.02") -> Quote:
    return Quote("AAPL", "iex", now - timedelta(seconds=5), Decimal(bid), 1, Decimal(ask), 1)


def inputs(now: datetime = MIDDAY, **over: object) -> ScanInputs:
    base = ScanInputs(symbol="AAPL", enabled=True, eligibility=ELIGIBLE, market_cap=None,
                      daily=history(), recent_bars=bars_until(now), quote=quote(now))
    return replace(base, **over)  # type: ignore[arg-type]


def run(inp: ScanInputs, now: datetime = MIDDAY, cfg: UniverseSettings = CFG, feed: str = "iex"):  # type: ignore[no-untyped-def]
    return evaluate(inp, cfg, feed=feed, now=now, session=SESSION)


def f(result, fid: str):  # type: ignore[no-untyped-def]
    return next(x for x in result.filters if x.id == fid)


def test_healthy_symbol_passes_every_filter() -> None:
    r = run(inputs())
    assert r.passed, r.reasons_json()
    assert [x.id for x in r.filters] == [f"U{i}" for i in range(1, 10)]
    assert r.metrics["adv"] == 50_000 and r.metrics["price"] == "101"
    assert r.metrics["change_pct"] == 1.0


def test_all_reasons_are_recorded_not_short_circuited() -> None:
    bad = Eligibility({"E1": RuleResult(True, "ok"), "E2": RuleResult(False, "asset type is ETF")})
    r = run(inputs(enabled=False, eligibility=bad, daily=history(3), quote=None))
    assert r.failed == ["U1", "U2", "U4", "U6"]
    assert "E2 (asset type is ETF)" in f(r, "U2").detail


def test_price_floor_and_hard_floor() -> None:
    r = run(inputs(recent_bars=bars_until(MIDDAY, close="9.99"),
                   quote=quote(MIDDAY, "9.98", "10.00")))
    assert not f(r, "U3").passed and f(r, "U3").limit == Decimal("10")
    # A config can't go below the $5 hard floor (the model rejects it anyway).
    low = UniverseSettings.model_construct(**{**CFG.model_dump(), "min_stock_price": Decimal("1")})
    r2 = run(inputs(recent_bars=bars_until(MIDDAY, close="4.50"),
                    quote=quote(MIDDAY, "4.49", "4.50")), cfg=low)
    assert not f(r2, "U3").passed and f(r2, "U3").limit == Decimal("5")


def test_price_falls_back_to_previous_close_before_the_open() -> None:
    r = run(inputs(PREMARKET, recent_bars=[], quote=None), now=PREMARKET)
    assert f(r, "U3").passed and f(r, "U3").value == Decimal("100")


def test_adv_threshold_and_history_requirement() -> None:
    assert not f(run(inputs(daily=history(volume=19_999))), "U4").passed
    short = f(run(inputs(daily=history(15))), "U4")  # needs 16 of 20
    assert not short.passed and "insufficient history: 15 of 16" in short.detail
    assert f(run(inputs(daily=history(16))), "U4").passed


def test_adv_uses_most_recent_sessions() -> None:
    days = history(25, volume=1_000)
    days[-20:] = [replace(d, volume=40_000) for d in days[-20:]]
    assert average_daily_volume(days, 20) == (40_000, 20)


def test_market_cap_only_when_entered() -> None:
    skipped = f(run(inputs()), "U5")
    assert skipped.passed and not skipped.evaluated
    assert not f(run(inputs(market_cap=Decimal("1500000000"))), "U5").passed
    assert f(run(inputs(market_cap=Decimal("2500000000"))), "U5").passed


@pytest.mark.parametrize(("bid", "ask", "feed", "ok"), [
    ("100.00", "100.25", "iex", True),     # ~0.25% ≤ 0.30%
    ("100.00", "100.40", "iex", False),    # ~0.40%
    ("100.00", "100.25", "sip", False),    # SIP threshold is 0.10%
])
def test_spread_is_feed_specific(bid: str, ask: str, feed: str, ok: bool) -> None:
    r = run(inputs(quote=quote(MIDDAY, bid, ask)), feed=feed)
    assert f(r, "U6").passed is ok


def test_spread_needs_fresh_two_sided_quote_during_session() -> None:
    stale = Quote("AAPL", "iex", MIDDAY - timedelta(minutes=5), Decimal("1"), 1,
                  Decimal("1.001"), 1)
    assert "quote is 300s old" in f(run(inputs(quote=stale)), "U6").detail
    crossed = f(run(inputs(quote=quote(MIDDAY, "101.10", "101.00"))), "U6")
    assert not crossed.passed and "crossed" in crossed.detail
    closed = f(run(inputs(PREMARKET, quote=None), now=PREMARKET), "U6")
    assert closed.passed and not closed.evaluated


def test_bar_coverage() -> None:
    sparse = f(run(inputs(recent_bars=bars_until(MIDDAY, every=3))), "U7")
    assert not sparse.passed and sparse.value == pytest.approx(10 / 30)
    early = OPEN + timedelta(minutes=5)
    assert not f(run(inputs(early, recent_bars=bars_until(early, 5)), now=early), "U7").evaluated


def test_suspicious_jump_is_flagged() -> None:
    r = run(inputs(recent_bars=bars_until(MIDDAY, close="125"),
                   quote=quote(MIDDAY, "124.98", "125.02")))
    assert not f(r, "U8").passed and "price jump 25.0%" in f(r, "U8").detail


def test_no_recent_trades_suggests_halt() -> None:
    old = [Bar("AAPL", "iex", MIDDAY - timedelta(minutes=20), *(Decimal("101"),) * 4, 500)]
    r = f(run(inputs(recent_bars=old)), "U9")
    assert not r.passed and "possibly halted" in r.detail
    none_today = f(run(inputs(recent_bars=[])), "U9")
    assert not none_today.passed and "no bars today" in none_today.detail


def test_session_dependent_filters_not_evaluated_when_closed() -> None:
    r = run(inputs(PREMARKET, recent_bars=[], quote=None), now=PREMARKET)
    assert r.passed
    assert {x.id for x in r.filters if not x.evaluated} == {"U5", "U6", "U7", "U8", "U9"}


def test_reasons_json_is_serialisable() -> None:
    import json

    out = run(inputs(daily=history(3))).reasons_json()
    json.dumps(out)
    assert out["U4"]["passed"] is False and out["U4"]["limit"] == 20_000
