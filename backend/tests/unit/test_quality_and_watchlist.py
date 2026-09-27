"""Data-quality checks, stream budget planning, and the seed watchlist."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from intraday.marketdata.calendar import MarketCalendar, SessionDay
from intraday.marketdata.quality import assess, bar_problems, coverage, in_regular_session
from intraday.marketdata.types import Bar
from intraday.marketdata.watchlist import Seed, SeedEntry, load_seed, stream_plan, validate_seed

SEED_PATH = Path(__file__).resolve().parents[3] / "config" / "watchlist.seed.yaml"
SESSION = SessionDay.from_alpaca({"date": "2026-09-21", "open": "09:30", "close": "16:00"})
CAL = MarketCalendar([SESSION])
OPEN = SESSION.open_at  # 13:30 UTC


def bar(symbol: str, ts: datetime, close: str = "100") -> Bar:
    c = Decimal(close)
    return Bar(symbol, "iex", ts, c, c + 1, c - 1, c, 100)


# ------------------------------------------------------------------ quality

def test_regular_session_filter() -> None:
    assert in_regular_session(bar("AAPL", OPEN), CAL)
    assert in_regular_session(bar("AAPL", SESSION.close_at - timedelta(minutes=1)), CAL)
    assert not in_regular_session(bar("AAPL", OPEN - timedelta(minutes=1)), CAL)   # pre-market
    assert not in_regular_session(bar("AAPL", SESSION.close_at), CAL)              # after hours
    assert not in_regular_session(bar("AAPL", datetime(2026, 9, 20, 15, tzinfo=UTC)), CAL)


def test_coverage_counts_completed_minutes_in_window() -> None:
    now = OPEN + timedelta(minutes=40, seconds=20)
    times = [OPEN + timedelta(minutes=m) for m in range(10, 40, 2)]  # every other minute
    assert coverage(times, SESSION, now) == pytest.approx(0.5)
    assert coverage([], SESSION, OPEN + timedelta(seconds=30)) == 1.0  # nothing expected yet


@pytest.mark.parametrize(("o", "h", "low", "c", "problem"), [
    ("0", "1", "0", "1", "non-positive price"),
    ("10", "9", "8", "10", "inconsistent OHLC"),
    ("10", "12", "11", "10", "inconsistent OHLC"),
])
def test_bar_problems(o: str, h: str, low: str, c: str, problem: str) -> None:
    b = Bar("X", "iex", OPEN, Decimal(o), Decimal(h), Decimal(low), Decimal(c), 1)
    assert problem in bar_problems(b)


def test_price_jump_flagged() -> None:
    assert any("jump" in p for p in bar_problems(bar("X", OPEN, "112"), Decimal("100")))
    assert bar_problems(bar("X", OPEN, "105"), Decimal("100")) == []


def health(latest: dict[str, Bar], now: datetime, *, connected: bool = True):  # type: ignore[no-untyped-def]
    return assess(latest, ["SPY", "QQQ", "AAPL"], CAL, now, 120, stream_connected=connected)


def test_fresh_market_data() -> None:
    now = OPEN + timedelta(minutes=10, seconds=5)
    latest = {s: bar(s, now - timedelta(minutes=1, seconds=5)) for s in ("SPY", "QQQ", "AAPL")}
    h = health(latest, now)
    assert h.market_open and not h.stale and h.stale_symbols == []


def test_stale_reference_symbol_blocks() -> None:
    now = OPEN + timedelta(minutes=10)
    latest = {"SPY": bar("SPY", now - timedelta(minutes=1)),
              "QQQ": bar("QQQ", now - timedelta(minutes=5))}
    h = health(latest, now)
    assert h.stale and any("QQQ" in r for r in h.reasons)
    assert h.stale_symbols == ["AAPL", "QQQ"]  # AAPL: no bar yet → excluded, not market-wide


def test_disconnected_stream_blocks() -> None:
    now = OPEN + timedelta(minutes=10)
    latest = {s: bar(s, now - timedelta(minutes=1)) for s in ("SPY", "QQQ")}
    assert health(latest, now, connected=False).reasons == ["market-data stream disconnected"]


def test_no_staleness_outside_session_or_during_warmup() -> None:
    assert not health({}, OPEN - timedelta(hours=1)).stale                 # pre-market
    assert not health({}, OPEN + timedelta(minutes=1)).stale               # warm-up
    assert not health({}, SESSION.close_at + timedelta(minutes=5)).stale   # after close


# ------------------------------------------------------------------ watchlist

@dataclass
class Row:
    symbol: str
    role: str
    asset_type: str = "COMMON_STOCK"
    country: str | None = "US"
    sector: str | None = None
    sector_etf: str | None = None
    enabled: bool = True
    alpaca_class: str | None = None
    alpaca_exchange: str | None = None
    alpaca_name: str | None = None
    alpaca_tradable: bool | None = None
    alpaca_status: str | None = None
    alpaca_synced_at: datetime | None = None


def ctx(symbol: str, sector: str | None = None) -> Row:
    return Row(symbol, "CONTEXT", "ETF", sector=sector)


def test_stream_plan_priority_and_budget() -> None:
    rows = [ctx("VIXY"), ctx("QQQ"), ctx("SPY"), ctx("XLK", "Technology"), ctx("XLE", "Energy"),
            Row("MSFT", "TRADABLE", sector_etf="XLK"), Row("AAPL", "TRADABLE", sector_etf="XLK"),
            Row("TSM", "TRADABLE", country="TW"),       # fails E3 → never streamed
            Row("OFF", "TRADABLE", enabled=False)]
    plan = stream_plan(rows, 30)
    # SPY/QQQ first, other core, then referenced sector ETFs only (not XLE), then stocks.
    assert plan.subscribed == ["SPY", "QQQ", "VIXY", "XLK", "AAPL", "MSFT"]
    small = stream_plan(rows, 4)
    assert small.subscribed == ["SPY", "QQQ", "VIXY", "XLK"] and small.dropped == ["AAPL", "MSFT"]


def test_seed_file_is_valid_and_within_free_plan_budget() -> None:
    seed = load_seed(SEED_PATH)
    assert validate_seed(seed, 30) == []
    assert {"SPY", "QQQ"} <= {e.symbol for e in seed.context}
    assert all(e.role == "TRADABLE" and e.asset_type == "COMMON_STOCK" and e.country == "US"
               for e in seed.tradable)
    assert all(e.role == "CONTEXT" for e in seed.context)
    rows = [Row(e.symbol, e.role, e.asset_type, e.country, e.sector, e.sector_etf)
            for e in seed.all]
    plan = stream_plan(rows, 30)
    assert plan.dropped == [] and len(plan.subscribed) <= 30


def test_validate_seed_catches_problems() -> None:
    seed = Seed(context=[SeedEntry("SPY", "CONTEXT", "ETF", "US")],
                tradable=[SeedEntry("TQQQ", "TRADABLE", "ETF", "US"),
                          SeedEntry("AAPL", "TRADABLE", "COMMON_STOCK", "US", sector_etf="XLK")])
    problems = validate_seed(seed, 30)
    assert any("TQQQ: fails E2" in p for p in problems)
    assert any("XLK is not a context symbol" in p for p in problems)
