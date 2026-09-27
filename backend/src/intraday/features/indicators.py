"""Technical indicators (docs/design/02 T8, 09 §2). Pure functions over sequences.

Conventions:
- Output lists align with the input; positions without enough data are ``None``.
- A value at index ``i`` depends only on inputs ``0..i`` (no look-ahead).
- Float math (indicators are statistics, not money; see design T12).
- EMA is seeded with the SMA of the first ``n`` values; RSI and ATR use Wilder smoothing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Series = list[float | None]


def sma(values: Sequence[float], n: int) -> Series:
    _check(n)
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: Sequence[float], n: int) -> Series:
    _check(n)
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = prev + alpha * (values[i] - prev)
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    """Wilder's RSI. All-flat input → 50; only gains → 100; only losses → 0."""
    _check(n)
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n + 1, len(closes)):
        avg_gain = (avg_gain * (n - 1) + gains[i - 1]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i - 1]) / n
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[Series, Series, Series]:
    """(macd line, signal line, histogram)."""
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")
    fast_e, slow_e = ema(closes, fast), ema(closes, slow)
    line: Series = [f - s if f is not None and s is not None else None
                    for f, s in zip(fast_e, slow_e, strict=True)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig: Series = [None] * len(closes)
    if first is not None:
        tail = ema([v for v in line[first:] if v is not None], signal)
        sig[first:] = tail
    hist: Series = [m - s if m is not None and s is not None else None
                    for m, s in zip(line, sig, strict=True)]
    return line, sig, hist


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
               ) -> list[float]:
    out = []
    for i in range(len(highs)):
        if i == 0:
            out.append(highs[0] - lows[0])
        else:
            pc = closes[i - 1]
            out.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14
        ) -> Series:
    """Wilder's ATR: first value = mean of the first n true ranges."""
    _check(n)
    tr = true_range(highs, lows, closes)
    out: Series = [None] * len(tr)
    if len(tr) < n:
        return out
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(tr)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def roc(closes: Sequence[float], n: int) -> Series:
    """Rate of change in percent vs ``n`` bars ago."""
    _check(n)
    return [None if i < n or closes[i - n] == 0 else (closes[i] / closes[i - n] - 1) * 100
            for i in range(len(closes))]


def cumulative_vwap(prices: Sequence[float], volumes: Sequence[float]) -> Series:
    """Session VWAP: Σ price·volume / Σ volume, cumulative from the first bar."""
    out: Series = []
    pv = vol = 0.0
    for p, v in zip(prices, volumes, strict=True):
        pv += p * v
        vol += v
        out.append(pv / vol if vol > 0 else None)
    return out


def realized_vol(closes: Sequence[float]) -> float | None:
    """Sample standard deviation of log returns (per bar, not annualised)."""
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def slope(values: Sequence[float]) -> float | None:
    """Least-squares slope per step."""
    n = len(values)
    if n < 2:
        return None
    mx = (n - 1) / 2
    my = sum(values) / n
    den = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (v - my) for i, v in enumerate(values)) / den


def _check(n: int) -> None:
    if n < 1:
        raise ValueError("period must be >= 1")
