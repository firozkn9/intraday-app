"""Market-data value types, independent of any provider."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from intraday.core.timeutil import ET

Feed = Literal["iex", "sip"]
Timeframe = Literal["1Min", "1Day"]
BAR_INTERVAL = timedelta(minutes=1)


_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$")


def parse_ts(value: str) -> datetime:
    """RFC-3339 with optional nanoseconds (Alpaca) → aware UTC datetime (µs precision)."""
    m = _TS.match(value.strip())
    if m is None:
        raise ValueError(f"unrecognised timestamp {value!r}")
    base, frac, tz = m.groups()
    micro = (frac or "")[:6].ljust(6, "0")
    offset = "+00:00" if tz == "Z" else tz
    return datetime.fromisoformat(f"{base}.{micro}{offset}").astimezone(UTC)


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


@dataclass(frozen=True)
class Bar:
    symbol: str
    feed: str
    ts: datetime  # bar start, UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None = None
    trade_count: int | None = None
    is_updated: bool = False

    @classmethod
    def from_alpaca(cls, symbol: str, feed: str, raw: dict[str, Any], *,
                    is_updated: bool = False) -> Bar:
        return cls(symbol=symbol, feed=feed, ts=parse_ts(str(raw["t"])), open=_dec(raw["o"]),
                   high=_dec(raw["h"]), low=_dec(raw["l"]), close=_dec(raw["c"]),
                   volume=int(raw["v"]),
                   vwap=_dec(raw["vw"]) if raw.get("vw") is not None else None,
                   trade_count=int(raw["n"]) if raw.get("n") is not None else None,
                   is_updated=is_updated)

    @property
    def end(self) -> datetime:
        return self.ts + BAR_INTERVAL

    def to_row(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "feed": self.feed, "ts": self.ts, "open": self.open,
                "high": self.high, "low": self.low, "close": self.close, "volume": self.volume,
                "vwap": self.vwap, "trade_count": self.trade_count, "is_updated": self.is_updated}


def daily_session_date(ts: datetime) -> date:
    """Session date of a daily bar.

    Alpaca's docs don't state whether daily bars are stamped at midnight ET or UTC.
    Adding 12 hours before converting to ET yields the right date for either.
    """
    return (ts + timedelta(hours=12)).astimezone(ET).date()


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    feed: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None = None

    @classmethod
    def from_alpaca(cls, symbol: str, feed: str, raw: dict[str, Any]) -> DailyBar:
        b = Bar.from_alpaca(symbol, feed, raw)
        return cls(symbol, feed, daily_session_date(b.ts), b.open, b.high, b.low, b.close,
                   b.volume, b.vwap)

    def to_row(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "feed": self.feed, "session_date": self.session_date,
                "open": self.open, "high": self.high, "low": self.low, "close": self.close,
                "volume": self.volume, "vwap": self.vwap}


@dataclass(frozen=True)
class Quote:
    symbol: str
    feed: str
    ts: datetime
    bid: Decimal
    bid_size: int
    ask: Decimal
    ask_size: int

    @classmethod
    def from_alpaca(cls, symbol: str, feed: str, raw: dict[str, Any]) -> Quote:
        return cls(symbol, feed, parse_ts(str(raw["t"])), _dec(raw["bp"]), int(raw["bs"]),
                   _dec(raw["ap"]), int(raw["as"]))

    @property
    def crossed(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.bid > self.ask

    @property
    def spread_pct(self) -> Decimal | None:
        if self.bid <= 0 or self.ask <= 0 or self.crossed:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100
