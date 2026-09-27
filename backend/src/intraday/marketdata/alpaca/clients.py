"""Alpaca market-data (bars, quotes) and reference (assets, calendar, clock) clients.

Formats follow docs.alpaca.markets (verified 2026-09-26):
- GET {data}/v2/stocks/bars ?symbols&timeframe&start&end&limit<=10000&feed&adjustment
  &page_token
  → {"bars": {"SYM": [{"t","o","h","l","c","v","n","vw"}]}, "next_page_token": str|null}
- GET {data}/v2/stocks/quotes/latest ?symbols&feed
  → {"quotes": {"SYM": {"t","bp","bs","ap","as",…}}}
- GET {paper}/v2/assets/{symbol} → {"symbol","name","class","exchange","status","tradable",…}
- GET {paper}/v2/calendar ?start&end → [{"date","open":"HH:MM","close":"HH:MM",…}]
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from intraday.core.safety import assert_paper_mode
from intraday.marketdata.alpaca.http import AlpacaError, AlpacaHttp
from intraday.marketdata.types import Quote

MAX_PAGE_LIMIT = 10_000
SYMBOLS_PER_REQUEST = 10


def _rfc3339(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


class AlpacaDataClient:
    def __init__(self, http: AlpacaHttp) -> None:
        self.http = http

    async def get_bars(self, symbols: Sequence[str], timeframe: str, start: datetime,
                       end: datetime, feed: str) -> dict[str, list[dict[str, Any]]]:
        """All raw bars for ``symbols`` in [start, end], following pagination."""
        out: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        for i in range(0, len(symbols), SYMBOLS_PER_REQUEST):
            chunk = list(symbols[i:i + SYMBOLS_PER_REQUEST])
            params: dict[str, Any] = {
                "symbols": ",".join(chunk), "timeframe": timeframe, "start": _rfc3339(start),
                "end": _rfc3339(end), "limit": MAX_PAGE_LIMIT, "feed": feed,
                "adjustment": "raw", "sort": "asc",
            }
            while True:
                body = await self.http.get("/v2/stocks/bars", params)
                for sym, bars in (body.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(bars or [])
                token = body.get("next_page_token")
                if not token:
                    break
                params = {**params, "page_token": token}
        return out

    async def get_news(self, symbols: Sequence[str], start: datetime, limit: int = 50
                       ) -> list[dict[str, Any]]:
        """GET {data}/v1beta1/news ?symbols&start&limit<=50&sort=desc&page_token
        → {"news": [{"id","headline","summary","source","url","symbols","created_at"}],
           "next_page_token"}. One page per call is enough for a 2-minute poll."""
        body = await self.http.get("/v1beta1/news", {
            "symbols": ",".join(symbols), "start": _rfc3339(start), "limit": min(limit, 50),
            "sort": "desc", "include_content": "false"})
        return list(body.get("news") or [])

    async def get_latest_quotes(self, symbols: Sequence[str], feed: str) -> dict[str, Quote]:
        body = await self.http.get("/v2/stocks/quotes/latest",
                                   {"symbols": ",".join(symbols), "feed": feed})
        return {sym: Quote.from_alpaca(sym, feed, q)
                for sym, q in (body.get("quotes") or {}).items()}


class AlpacaReferenceClient:
    """Read-only calls to the Trading API. Constructed only against the paper endpoint."""

    def __init__(self, http: AlpacaHttp, *, trading_mode: str) -> None:
        assert_paper_mode(trading_mode, http.base_url)
        self.http = http

    async def get_asset(self, symbol: str) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = await self.http.get(f"/v2/assets/{symbol}")
        except AlpacaError as exc:
            if exc.status in (404, 422):
                return None
            raise
        return result

    async def get_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self.http.get(
            "/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()})
        return result

    async def get_clock(self) -> dict[str, Any]:
        result: dict[str, Any] = await self.http.get("/v2/clock")
        return result
