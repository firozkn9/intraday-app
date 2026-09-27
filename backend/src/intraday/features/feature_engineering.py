"""Feature engineering (docs/design/09 §2).

Two layers:
- ``SymbolContext``: per-symbol, per-session inputs computed once before/at the
  open (previous close, daily ATR, ADV, intraday volume profile, warm-up bars).
- ``compute_features``: per completed bar, from today's bars up to that bar
  (never later ones) plus the context.

Relative volume compares today's cumulative volume with the average cumulative
volume at the same minute over recent sessions **from the same feed**
(CLAUDE.md), so switching IEX→SIP doesn't silently shift thresholds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from intraday.features import indicators as ind
from intraday.marketdata.calendar import SessionDay
from intraday.marketdata.types import BAR_INTERVAL, Bar

SESSION_MINUTES = 390
PROFILE_MIN_SESSIONS = 10
VOLUME_SPIKE_LOOKBACK = 20
VOLUME_TREND_BARS = 10
RV_WINDOW = 30


@dataclass(frozen=True)
class DailyStat:
    session_date: date
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class SymbolContext:
    symbol: str
    prev_close: float | None = None
    prev_high: float | None = None
    prev_low: float | None = None
    atr14_daily: float | None = None
    adv20: float | None = None
    volume_profile: tuple[float, ...] | None = None   # avg cumulative volume per minute index
    warmup: tuple[Bar, ...] = ()                       # previous session's bars
    rv30_history: tuple[float, ...] = ()               # historical 30-bar realized vols


def minute_index(ts: Any, session: SessionDay) -> int:
    return int((ts - session.open_at) / BAR_INTERVAL)


def cumulative_profile(session_bars: Sequence[Bar], session: SessionDay) -> list[float]:
    """Cumulative volume at the end of each session minute (missing bar = no volume)."""
    per_min = [0.0] * SESSION_MINUTES
    for b in session_bars:
        i = minute_index(b.ts, session)
        if 0 <= i < SESSION_MINUTES:
            per_min[i] += b.volume
    out, total = [], 0.0
    for v in per_min:
        total += v
        out.append(total)
    return out


def rolling_rv_samples(closes: Sequence[float], window: int = RV_WINDOW) -> list[float]:
    """Non-overlapping ``window``-bar realized vols (for the regime's vol percentile)."""
    out = []
    for end in range(window, len(closes) + 1, window):
        rv = ind.realized_vol(closes[end - window:end])
        if rv is not None:
            out.append(rv)
    return out


def build_context(symbol: str, daily: Sequence[DailyStat],
                  history: Mapping[date, tuple[SessionDay, Sequence[Bar]]],
                  warmup: Sequence[Bar], *, adv_lookback: int = 20) -> SymbolContext:
    """``daily``/``history`` must contain only sessions *before* the one being traded."""
    days = sorted(daily, key=lambda d: d.session_date)
    prev = days[-1] if days else None
    atr_d = None
    if len(days) >= 15:
        a = ind.atr([d.high for d in days], [d.low for d in days], [d.close for d in days], 14)
        atr_d = a[-1]
    recent = days[-adv_lookback:]
    adv = sum(d.volume for d in recent) / len(recent) if recent else None

    profiles = [cumulative_profile(bars, sess) for sess, bars in history.values() if bars]
    profile = None
    if len(profiles) >= PROFILE_MIN_SESSIONS:
        profile = tuple(sum(p[i] for p in profiles) / len(profiles)
                        for i in range(SESSION_MINUTES))
    rv_hist: list[float] = []
    for _, bars in history.values():
        ordered = sorted(bars, key=lambda b: b.ts)
        rv_hist.extend(rolling_rv_samples([float(b.close) for b in ordered]))

    return SymbolContext(symbol, prev.close if prev else None, prev.high if prev else None,
                         prev.low if prev else None, atr_d, adv, profile,
                         tuple(sorted(warmup, key=lambda b: b.ts)), tuple(rv_hist))


@dataclass
class Features:
    """Scalar features at one bar (persisted as the decision's market-data snapshot)."""

    symbol: str
    bar_ts: str
    minute_index: int
    bars_today: int
    price: float
    session_open: float
    prev_close: float | None
    gap_pct: float | None
    intraday_return_pct: float
    vwap: float
    vwap_dist_pct: float
    day_high: float
    day_low: float
    day_range_pct: float
    range_5m_pct: float
    range_15m_pct: float
    atr14_1m: float | None
    atr_pct: float | None
    atr14_daily: float | None
    rsi14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    macd_hist_prev: float | None
    roc10: float | None
    ema9: float | None
    ema20: float | None
    ema50: float | None
    ema9_prev: float | None
    ema20_prev: float | None
    volume: float
    cum_volume: float
    rvol: float | None
    volume_spike: float | None
    volume_trend: float | None
    rising_volume_bars: int
    realized_vol_30m: float | None
    rv_percentile: float | None
    bars_below_vwap_before: int
    closes_above_vwap: int
    higher_highs: bool
    bar_green: bool
    adv20: float | None
    extra: dict[str, float | None] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in d.items()}


@dataclass(frozen=True)
class Series:
    """Aligned series for strategies that need look-backs (today's bars only)."""

    closes: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    volumes: tuple[float, ...]
    vwaps: tuple[float, ...]
    ema9: tuple[float | None, ...]
    ema20: tuple[float | None, ...]
    ema50: tuple[float | None, ...]


@dataclass(frozen=True)
class SymbolState:
    features: Features
    series: Series


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def _range_pct(highs: Sequence[float], lows: Sequence[float], price: float) -> float:
    return (max(highs) - min(lows)) / price * 100 if highs and price else 0.0


def compute_features(bars_today: Sequence[Bar], ctx: SymbolContext, session: SessionDay
                     ) -> SymbolState | None:
    """Features at the last bar in ``bars_today``; callers pass only completed bars."""
    today = sorted(bars_today, key=lambda b: b.ts)
    if not today:
        return None
    all_bars = list(ctx.warmup) + today
    offset = len(ctx.warmup)
    closes = [float(b.close) for b in all_bars]
    highs = [float(b.high) for b in all_bars]
    lows = [float(b.low) for b in all_bars]
    vols = [float(b.volume) for b in all_bars]

    t_closes, t_highs = closes[offset:], highs[offset:]
    t_lows, t_vols = lows[offset:], vols[offset:]
    typical = [float(b.vwap) if b.vwap is not None else (float(b.high) + float(b.low)
               + float(b.close)) / 3 for b in today]
    vwaps = ind.cumulative_vwap(typical, t_vols)
    vw_series = [v if v is not None else t_closes[i] for i, v in enumerate(vwaps)]

    e9, e20, e50 = ind.ema(closes, 9), ind.ema(closes, 20), ind.ema(closes, 50)
    r14 = ind.rsi(closes, 14)
    m_line, m_sig, m_hist = ind.macd(closes)
    a1m = ind.atr(highs, lows, closes, 14)
    r10 = ind.roc(closes, 10)

    last = today[-1]
    price = float(last.close)
    vwap = vw_series[-1]
    mi = minute_index(last.ts, session)
    cum_vol = sum(t_vols)
    rvol = None
    if ctx.volume_profile and 0 <= mi < SESSION_MINUTES and ctx.volume_profile[mi] > 0:
        rvol = cum_vol / ctx.volume_profile[mi]

    prev_vols = vols[-VOLUME_SPIKE_LOOKBACK - 1:-1]
    spike = vols[-1] / (sum(prev_vols) / len(prev_vols)) if prev_vols and sum(prev_vols) else None
    trend_vols = vols[-VOLUME_TREND_BARS:]
    mean_tv = sum(trend_vols) / len(trend_vols)
    vtrend = (ind.slope(trend_vols) or 0.0) / mean_tv if mean_tv else None

    rising = 0
    for i in range(len(t_vols) - 1, 0, -1):
        if t_vols[i] > t_vols[i - 1]:
            rising += 1
        else:
            break

    above = 0
    for i in range(len(today) - 1, -1, -1):
        if t_closes[i] > vw_series[i]:
            above += 1
        else:
            break
    below_before = 0
    for i in range(len(today) - 1 - above, -1, -1):
        if t_closes[i] < vw_series[i]:
            below_before += 1
        else:
            break

    blocks = [max(t_highs[i:i + 5]) for i in range(max(0, len(t_highs) - 15),
                                                    len(t_highs) - 4, 5)]
    higher_highs = len(blocks) == 3 and blocks[0] < blocks[1] < blocks[2]

    rv30 = ind.realized_vol(closes[-RV_WINDOW - 1:]) if len(closes) > RV_WINDOW else None
    rv_pct = None
    if rv30 is not None and len(ctx.rv30_history) >= 20:
        rv_pct = sum(1 for h in ctx.rv30_history if h <= rv30) / len(ctx.rv30_history) * 100

    atr1m = a1m[-1]
    feats = Features(
        symbol=last.symbol, bar_ts=last.ts.isoformat(), minute_index=mi, bars_today=len(today),
        price=price, session_open=float(today[0].open), prev_close=ctx.prev_close,
        gap_pct=_pct(float(today[0].open), ctx.prev_close) if ctx.prev_close else None,
        intraday_return_pct=_pct(price, float(today[0].open)),
        vwap=vwap, vwap_dist_pct=_pct(price, vwap),
        day_high=max(t_highs), day_low=min(t_lows),
        day_range_pct=_range_pct(t_highs, t_lows, price),
        range_5m_pct=_range_pct(t_highs[-5:], t_lows[-5:], price),
        range_15m_pct=_range_pct(t_highs[-15:], t_lows[-15:], price),
        atr14_1m=atr1m, atr_pct=atr1m / price * 100 if atr1m is not None else None,
        atr14_daily=ctx.atr14_daily, rsi14=r14[-1], macd=m_line[-1], macd_signal=m_sig[-1],
        macd_hist=m_hist[-1], macd_hist_prev=m_hist[-2] if len(m_hist) > 1 else None,
        roc10=r10[-1], ema9=e9[-1], ema20=e20[-1], ema50=e50[-1],
        ema9_prev=e9[-2] if len(e9) > 1 else None, ema20_prev=e20[-2] if len(e20) > 1 else None,
        volume=vols[-1], cum_volume=cum_vol, rvol=rvol, volume_spike=spike, volume_trend=vtrend,
        rising_volume_bars=rising, realized_vol_30m=rv30, rv_percentile=rv_pct,
        bars_below_vwap_before=below_before, closes_above_vwap=above,
        higher_highs=higher_highs, bar_green=float(last.close) > float(last.open),
        adv20=ctx.adv20,
    )
    series = Series(tuple(t_closes), tuple(t_highs), tuple(t_lows), tuple(t_vols),
                    tuple(vw_series), tuple(e9[offset:]), tuple(e20[offset:]),
                    tuple(e50[offset:]))
    return SymbolState(feats, series)
