"""Alpaca REST clients against recorded-format fixtures (respx; no network)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from intraday.core.safety import PaperModeViolation
from intraday.marketdata.alpaca.clients import AlpacaDataClient, AlpacaReferenceClient
from intraday.marketdata.alpaca.http import (
    DATA_BASE_URL,
    AlpacaError,
    AlpacaHttp,
    Credentials,
    TokenBucket,
)
from intraday.marketdata.types import Bar, DailyBar, daily_session_date
from tests.fakes import fixture

PAPER = "https://paper-api.alpaca.markets"
CREDS = Credentials("key-id", "secret-key")
START = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
END = datetime(2026, 9, 21, 21, 0, tzinfo=UTC)


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def http(base: str = DATA_BASE_URL, sleep: Sleeps | None = None) -> AlpacaHttp:
    return AlpacaHttp(base, CREDS, limiter=TokenBucket(10_000), sleep=sleep or Sleeps())


@respx.mock
async def test_bars_follow_pagination_and_send_auth_headers() -> None:
    route = respx.get(f"{DATA_BASE_URL}/v2/stocks/bars").mock(side_effect=[
        httpx.Response(200, json=fixture("bars_1min_page1.json")),
        httpx.Response(200, json=fixture("bars_1min_page2.json")),
    ])
    out = await AlpacaDataClient(http()).get_bars(["AAPL", "SPY"], "1Min", START, END, "iex")
    assert [len(out["AAPL"]), len(out["SPY"])] == [3, 3]
    first, second = route.calls[0].request, route.calls[1].request
    assert first.headers["APCA-API-KEY-ID"] == "key-id"
    assert first.headers["APCA-API-SECRET-KEY"] == "secret-key"
    assert first.url.params["feed"] == "iex" and first.url.params["timeframe"] == "1Min"
    assert first.url.params["start"] == "2026-09-21T12:00:00Z"
    assert "page_token" not in first.url.params
    assert second.url.params["page_token"] == "UEFHRTI="


@respx.mock
async def test_symbols_are_chunked() -> None:
    route = respx.get(f"{DATA_BASE_URL}/v2/stocks/bars").mock(
        return_value=httpx.Response(200, json={"bars": {}, "next_page_token": None}))
    symbols = [f"S{i:02d}" for i in range(23)]
    await AlpacaDataClient(http()).get_bars(symbols, "1Day", START, END, "iex")
    assert [len(c.request.url.params["symbols"].split(",")) for c in route.calls] == [10, 10, 3]


def test_bar_parsing_keeps_decimals_and_nanosecond_timestamps() -> None:
    raw = fixture("bars_1min_page1.json")["bars"]["AAPL"][2]
    bar = Bar.from_alpaca("AAPL", "iex", raw)
    assert bar.ts == datetime(2026, 9, 21, 13, 31, tzinfo=UTC)
    assert bar.close == Decimal("229.1") and bar.volume == 4020 and bar.trade_count == 71
    assert bar.end == datetime(2026, 9, 21, 13, 32, tzinfo=UTC)


@pytest.mark.parametrize("stamp", ["2026-09-21T04:00:00Z", "2026-09-21T05:00:00Z",
                                   "2026-09-21T00:00:00Z"])
def test_daily_bar_date_is_robust_to_timestamp_convention(stamp: str) -> None:
    raw = {"t": stamp, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
    assert DailyBar.from_alpaca("AAPL", "iex", raw).session_date == date(2026, 9, 21)
    assert daily_session_date(datetime(2026, 9, 21, 4, tzinfo=UTC)) == date(2026, 9, 21)


@respx.mock
async def test_latest_quotes() -> None:
    respx.get(f"{DATA_BASE_URL}/v2/stocks/quotes/latest").mock(
        return_value=httpx.Response(200, json=fixture("quotes_latest.json")))
    q = (await AlpacaDataClient(http()).get_latest_quotes(["AAPL"], "iex"))["AAPL"]
    assert (q.bid, q.ask, q.crossed) == (Decimal("229.08"), Decimal("229.12"), False)
    assert q.spread_pct is not None and q.spread_pct < Decimal("0.02")


@respx.mock
async def test_retries_429_honouring_retry_after_then_succeeds() -> None:
    sleeps = Sleeps()
    respx.get(f"{DATA_BASE_URL}/v2/stocks/quotes/latest").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "3"}, json={"message": "too many requests"}),
        httpx.Response(503),
        httpx.Response(200, json={"quotes": {}}),
    ])
    assert await AlpacaDataClient(http(sleep=sleeps)).get_latest_quotes(["AAPL"], "iex") == {}
    assert sleeps.calls[0] == 3.0 and len(sleeps.calls) == 2


@respx.mock
async def test_network_errors_are_retried_then_surface() -> None:
    sleeps = Sleeps()
    respx.get(f"{DATA_BASE_URL}/v2/stocks/quotes/latest").mock(
        side_effect=httpx.ConnectError("boom"))
    with pytest.raises(AlpacaError, match="network error"):
        await AlpacaDataClient(http(sleep=sleeps)).get_latest_quotes(["AAPL"], "iex")
    assert len(sleeps.calls) == 4  # max_retries


@respx.mock
async def test_client_errors_are_not_retried() -> None:
    sleeps = Sleeps()
    respx.get(f"{DATA_BASE_URL}/v2/stocks/bars").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"}))
    with pytest.raises(AlpacaError) as exc:
        await AlpacaDataClient(http(sleep=sleeps)).get_bars(["AAPL"], "1Min", START, END, "sip")
    assert exc.value.status == 403 and sleeps.calls == []


@respx.mock
async def test_reference_client_assets_and_calendar() -> None:
    respx.get(f"{PAPER}/v2/assets/AAPL").mock(
        return_value=httpx.Response(200, json=fixture("asset_AAPL.json")))
    respx.get(f"{PAPER}/v2/assets/NOPE").mock(
        return_value=httpx.Response(404, json={"message": "asset not found"}))
    cal_route = respx.get(f"{PAPER}/v2/calendar").mock(
        return_value=httpx.Response(200, json=fixture("calendar.json")))
    ref = AlpacaReferenceClient(http(PAPER), trading_mode="PAPER")
    assert (await ref.get_asset("AAPL"))["exchange"] == "NASDAQ"
    assert await ref.get_asset("NOPE") is None
    days = await ref.get_calendar(date(2026, 9, 17), date(2026, 9, 22))
    assert len(days) == 4
    assert cal_route.calls[0].request.url.params["start"] == "2026-09-17"


@pytest.mark.parametrize(("mode", "url"), [("PAPER", "https://api.alpaca.markets"),
                                           ("LIVE", PAPER)])
def test_reference_client_refuses_non_paper(mode: str, url: str) -> None:
    with pytest.raises(PaperModeViolation):
        AlpacaReferenceClient(http(url), trading_mode=mode)


async def test_token_bucket_paces_requests() -> None:
    now = [0.0]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    bucket = TokenBucket(60, clock=lambda: now[0], sleep=sleep)  # 1 per second
    for _ in range(60):
        await bucket.acquire()
    assert slept == []
    await bucket.acquire()
    assert slept and abs(sum(slept) - 1.0) < 1e-6


def test_credentials_never_render_the_secret() -> None:
    assert "secret-key" not in repr(CREDS) and "secret-key" not in str(CREDS)
