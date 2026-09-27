"""Synthetic sessions for strategy/pipeline tests (deterministic, no randomness)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from intraday.features.feature_engineering import SymbolContext
from intraday.marketdata.calendar import SessionDay
from intraday.marketdata.types import Bar

SESSION = SessionDay.from_alpaca({"date": "2026-09-21", "open": "09:30", "close": "16:00"})
PREV = SessionDay.from_alpaca({"date": "2026-09-18", "open": "09:30", "close": "16:00"})


def bars(symbol: str, closes: Sequence[float], volumes: Sequence[float] | float = 1000.0,
         session: SessionDay = SESSION, start_minute: int = 0, wick: float = 0.05) -> list[Bar]:
    """One bar per minute from the open; open = previous close, small symmetric wicks."""
    vols = [volumes] * len(closes) if isinstance(volumes, (int, float)) else list(volumes)
    out, prev = [], closes[0]
    for i, (c, v) in enumerate(zip(closes, vols, strict=True)):
        o = prev
        hi, lo = max(o, c) + wick, min(o, c) - wick
        d = lambda x: Decimal(str(round(x, 4)))  # noqa: E731
        out.append(Bar(symbol, "iex", session.open_at + timedelta(minutes=start_minute + i),
                       d(o), d(hi), d(lo), d(c), int(v), d((hi + lo + c) / 3)))
        prev = c
    return out


def context(symbol: str, *, warm_close: float = 100.0, per_minute_volume: float = 500.0,
            prev_close: float | None = None) -> SymbolContext:
    """Flat previous session for indicator warm-up, and a flat volume profile."""
    warm = bars(symbol, [warm_close] * 60, 1000.0, session=PREV, start_minute=330)
    profile = tuple(per_minute_volume * (i + 1) for i in range(390))
    return SymbolContext(symbol, prev_close if prev_close is not None else warm_close,
                         warm_close + 1, warm_close - 1, 2.0, 50_000.0, profile, tuple(warm))


def uptrend(n: int, start: float = 100.0, step: float = 0.03) -> list[float]:
    return [start + step * i for i in range(n)]


def market_up(n: int) -> dict[str, list[Bar]]:
    """SPY/QQQ steadily rising (bullish regime inputs)."""
    return {"SPY": bars("SPY", uptrend(n, 570.0, 0.05)),
            "QQQ": bars("QQQ", uptrend(n, 480.0, 0.05))}


def market_down(n: int) -> dict[str, list[Bar]]:
    return {"SPY": bars("SPY", uptrend(n, 570.0, -0.05)),
            "QQQ": bars("QQQ", uptrend(n, 480.0, -0.05))}


def breakout_day() -> list[float]:
    """Gentle uptrend with a 15-bar consolidation, then a breakout on the last bar."""
    base = uptrend(25, 100.0, 0.04)            # 100.00 → 100.96
    flat = [101.0 + (0.05 if i % 2 else -0.05) for i in range(15)]
    return base + flat + [101.1, 101.25, 101.5]


def breakout_volumes() -> list[float]:
    return [1000.0] * 40 + [1300.0, 1800.0, 2600.0]


def reclaim_day() -> list[float]:
    """Opens, sells off below VWAP for a while, then reclaims it on the last bars."""
    down = [100.0 - 0.08 * i for i in range(20)]          # 100.00 → 98.48
    flat = [98.5 + (0.03 if i % 2 else -0.03) for i in range(10)]
    up = [98.8, 99.2, 99.6, 100.0, 100.4]
    return down + flat + up


def pullback_day() -> list[float]:
    """Strong trend (EMAs aligned), controlled dip to EMA20, then a green bounce."""
    trend = uptrend(45, 100.0, 0.06)                        # → 102.64
    dip = [102.55, 102.45, 102.35, 102.30]
    return trend + dip + [102.45]


def pullback_volumes() -> list[float]:
    return [1000.0] * 49 + [2200.0]
