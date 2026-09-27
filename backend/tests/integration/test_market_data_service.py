"""MarketDataService against a real database, with Alpaca mocked (respx + fixtures)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.db.models import Bar1d, Bar1m, MarketCalendarDay, Symbol
from intraday.db.repositories import marketdata as md_repo
from intraday.db.session import transaction
from intraday.marketdata.alpaca.clients import AlpacaDataClient, AlpacaReferenceClient
from intraday.marketdata.alpaca.http import DATA_BASE_URL, AlpacaHttp, Credentials, TokenBucket
from intraday.marketdata.alpaca.stream import AlpacaBarStream, StreamEvent
from intraday.marketdata.service import MarketDataService
from intraday.marketdata.types import Bar
from tests.fakes import fixture

PAPER = "https://paper-api.alpaca.markets"
NOW = datetime(2026, 9, 21, 13, 40, tzinfo=UTC)  # Monday 09:40 ET
CREDS = Credentials("key", "secret")


async def no_sleep(_: float) -> None:
    return None


def make_service(sm: async_sessionmaker[AsyncSession], now: datetime = NOW) -> MarketDataService:
    data = AlpacaDataClient(AlpacaHttp(DATA_BASE_URL, CREDS, limiter=TokenBucket(100_000),
                                       sleep=no_sleep))
    ref = AlpacaReferenceClient(AlpacaHttp(PAPER, CREDS, limiter=TokenBucket(100_000),
                                           sleep=no_sleep), trading_mode="PAPER")
    stream = AlpacaBarStream(CREDS, "iex", url="ws://unused.invalid")
    return MarketDataService(sm, data, ref, stream, feed="iex", stream_limit=30,
                             clock=lambda: now)


async def seed(sm: async_sessionmaker[AsyncSession]) -> None:
    async with transaction(sm) as s:
        await md_repo.seed_symbols(s, [
            {"symbol": "SPY", "role": "CONTEXT", "asset_type": "ETF", "country": "US"},
            {"symbol": "AAPL", "role": "TRADABLE", "asset_type": "COMMON_STOCK", "country": "US"},
            # Entered by mistake as common stock: Alpaca's name gives it away (E7).
            {"symbol": "TQQQ", "role": "TRADABLE", "asset_type": "COMMON_STOCK", "country": "US"},
            {"symbol": "GONE", "role": "TRADABLE", "asset_type": "COMMON_STOCK", "country": "US"},
        ], only_if_empty=False)


def bars_router(request: httpx.Request) -> httpx.Response:
    """Serves fixtures like Alpaca would: only the requested symbols."""
    params = request.url.params
    wanted = set(params["symbols"].split(","))
    if params["timeframe"] == "1Day":
        body = fixture("bars_1day.json")
    elif params.get("page_token") == "UEFHRTI=":
        body = fixture("bars_1min_page2.json")
    elif params["start"].startswith("2026-09-21T13:3"):  # incremental follow-up: nothing new
        body = {"bars": {}, "next_page_token": None}
    else:
        body = fixture("bars_1min_page1.json")
    body["bars"] = {k: v for k, v in body["bars"].items() if k in wanted}
    if not body["bars"] and body.get("next_page_token"):
        body["next_page_token"] = None
    return httpx.Response(200, json=body)


@pytest.fixture
def alpaca() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{PAPER}/v2/calendar").mock(
            return_value=httpx.Response(200, json=fixture("calendar.json")))
        for sym in ("AAPL", "SPY", "TQQQ"):
            router.get(f"{PAPER}/v2/assets/{sym}").mock(
                return_value=httpx.Response(200, json=fixture(f"asset_{sym}.json")))
        router.get(f"{PAPER}/v2/assets/GONE").mock(return_value=httpx.Response(404, json={}))
        router.get(f"{DATA_BASE_URL}/v2/stocks/bars", name="bars").mock(side_effect=bars_router)
        yield router


async def test_calendar_sync_persists_sessions(sessionmaker: Any, alpaca: Any) -> None:
    svc = make_service(sessionmaker)
    assert await svc.sync_calendar() == 4
    async with sessionmaker() as s:
        rows = (await s.execute(select(MarketCalendarDay).order_by(
            MarketCalendarDay.session_date))).scalars().all()
    assert rows[0].session_date == date(2026, 9, 17)
    assert svc.calendar.is_open(NOW)
    # A second service can rebuild the calendar from the DB alone.
    other = make_service(sessionmaker)
    await other.load_calendar()
    assert other.calendar.session(date(2026, 9, 21)) is not None


async def test_asset_sync_computes_and_stores_eligibility(sessionmaker: Any, alpaca: Any) -> None:
    await seed(sessionmaker)
    result = await make_service(sessionmaker).sync_assets()
    assert result == {"AAPL": True, "GONE": False, "SPY": False, "TQQQ": False}
    async with sessionmaker() as s:
        rows = {r.symbol: r for r in (await s.execute(select(Symbol))).scalars()}
    assert rows["AAPL"].alpaca_exchange == "NASDAQ" and rows["AAPL"].eligibility["eligible"]
    assert rows["TQQQ"].eligibility["rules"]["E7"]["passed"] is False
    assert rows["GONE"].alpaca_status == "not_found"
    assert rows["SPY"].eligibility["rules"]["E1"]["passed"] is False  # context: never tradable


async def test_backfill_stores_regular_session_bars_and_is_incremental(
        sessionmaker: Any, alpaca: Any) -> None:
    await seed(sessionmaker)
    svc = make_service(sessionmaker)
    await svc.sync_calendar()
    plan = await svc.refresh_watchlist()
    assert plan.subscribed == ["SPY", "AAPL", "GONE", "TQQQ"]

    report = await svc.backfill(days=2)
    assert report.minute_bars == 4               # AAPL x2, SPY x2 in regular hours
    assert report.skipped_outside_session == 2   # AAPL pre-market, SPY after-hours
    assert report.daily_bars == 3
    async with sessionmaker() as s:
        minute = (await s.execute(select(Bar1m).order_by(Bar1m.symbol, Bar1m.ts))).scalars().all()
        daily = (await s.execute(select(Bar1d))).scalars().all()
    assert [(b.symbol, b.ts.minute) for b in minute] == [("AAPL", 30), ("AAPL", 31),
                                                         ("SPY", 30), ("SPY", 31)]
    assert all(b.feed == "iex" for b in minute)
    assert {d.session_date for d in daily} == {date(2026, 9, 18), date(2026, 9, 21)}
    assert svc.latest["SPY"].close == Decimal("571.8")

    # Second run resumes after the latest stored bar per symbol.
    calls_before = len(alpaca["bars"].calls)
    report2 = await svc.backfill(days=2)
    minute_calls = [c.request.url.params for c in alpaca["bars"].calls[calls_before:]
                    if c.request.url.params["timeframe"] == "1Min"]
    starts = {frozenset(p["symbols"].split(",")): p["start"] for p in minute_calls}
    assert starts[frozenset({"AAPL", "SPY"})] == "2026-09-21T13:32:00Z"
    # Symbols that never had data start at the window start.
    assert starts[frozenset({"GONE", "TQQQ"})] == "2026-09-18T13:30:00Z"
    assert report2.minute_bars == 0  # nothing new for anyone


async def test_stream_events_are_stored_and_trigger_gap_fill(sessionmaker: Any,
                                                             alpaca: Any) -> None:
    await seed(sessionmaker)
    svc = make_service(sessionmaker)
    await svc.sync_calendar()
    await svc.refresh_watchlist()

    live = Bar("SPY", "iex", datetime(2026, 9, 21, 13, 39, tzinfo=UTC), Decimal("572"),
               Decimal("572.2"), Decimal("571.9"), Decimal("572.1"), 900)
    pre = Bar("SPY", "iex", datetime(2026, 9, 21, 12, 0, tzinfo=UTC), Decimal("570"),
              Decimal("570"), Decimal("570"), Decimal("570"), 10)
    await svc.on_stream_event(StreamEvent("bar", bar=live))
    await svc.on_stream_event(StreamEvent("bar", bar=pre))  # pre-market: ignored
    corrected = Bar(live.symbol, live.feed, live.ts, live.open, live.high, live.low,
                    Decimal("572.05"), 950, is_updated=True)
    await svc.on_stream_event(StreamEvent("updated_bar", bar=corrected))
    async with sessionmaker() as s:
        rows = (await s.execute(select(Bar1m).where(Bar1m.symbol == "SPY"))).scalars().all()
    assert len(rows) == 1 and rows[0].is_updated and rows[0].close == Decimal("572.05")
    assert svc.latest["SPY"].close == Decimal("572.05")

    await svc.on_stream_event(StreamEvent("connected"))
    await svc.wait_idle()
    assert svc.last_backfill is not None and svc.last_backfill.error is None


async def test_health_and_snapshot(sessionmaker: Any, alpaca: Any) -> None:
    await seed(sessionmaker)
    svc = make_service(sessionmaker)
    await svc.sync_calendar()
    await svc.refresh_watchlist()
    h = svc.health(NOW, 120)
    assert h.stale and "market-data stream disconnected" in h.reasons
    snap = svc.snapshot(NOW, 120)
    assert snap["enabled"] and snap["plan"]["used"] == 4 and snap["stream"]["state"] == "idle"


async def test_upsert_tolerates_duplicate_bars_in_one_batch(sessionmaker: Any) -> None:
    ts = datetime(2026, 9, 21, 13, 30, tzinfo=UTC)
    first = Bar("AAPL", "iex", ts, Decimal("1"), Decimal("2"), Decimal("1"), Decimal("1.5"), 10)
    second = Bar("AAPL", "iex", ts, Decimal("1"), Decimal("2"), Decimal("1"), Decimal("1.7"), 12)
    async with transaction(sessionmaker) as s:
        assert await md_repo.upsert_bars(s, [first, second]) == 1
    async with sessionmaker() as s:
        row = (await s.execute(select(Bar1m))).scalar_one()
    assert row.close == Decimal("1.7")  # last one wins
