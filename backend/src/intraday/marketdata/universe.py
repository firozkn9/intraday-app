"""Universe filters (SPEC §7; docs/design/06 §3.4, 07 R5-R7). Pure: no I/O.

Each tradable watchlist symbol is checked against every filter, and every
result is recorded (not short-circuited) so the scanner can show all reasons.

A filter that can't be evaluated right now (e.g. spread while the market is
closed) passes with ``evaluated=False``; the risk engine re-checks those
conditions at order time (R6 data quality, R7 spread).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from intraday.core.assets import Eligibility
from intraday.core.hard_limits import HARD_MIN_STOCK_PRICE
from intraday.core.runtime_config import UniverseSettings
from intraday.marketdata.calendar import SessionDay
from intraday.marketdata.quality import bar_problems, coverage
from intraday.marketdata.types import Bar, Quote

MIN_HISTORY_FRACTION = 0.8          # ADV needs ≥ 80% of the lookback sessions
QUOTE_MAX_AGE = timedelta(seconds=60)
COVERAGE_MIN_ELAPSED = timedelta(minutes=10)
ACTIVITY_GRACE = timedelta(minutes=5)
NO_TRADES_SUSPECT = timedelta(minutes=5)   # no bar for this long → possible halt

Phase = Literal["closed", "open"]


@dataclass(frozen=True)
class DailyVolume:
    session_date: date
    volume: int
    close: Decimal


@dataclass(frozen=True)
class ScanInputs:
    symbol: str
    enabled: bool
    eligibility: Eligibility
    market_cap: Decimal | None
    daily: Sequence[DailyVolume]              # completed sessions before today, any order
    recent_bars: Sequence[Bar]                # today's regular-session bars (may be empty)
    quote: Quote | None


@dataclass(frozen=True)
class FilterResult:
    id: str
    name: str
    passed: bool
    detail: str
    value: Any = None
    limit: Any = None
    evaluated: bool = True

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "passed": self.passed, "detail": self.detail,
                "value": _j(self.value), "limit": _j(self.limit), "evaluated": self.evaluated}


def _j(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, float):
        return round(v, 6)
    return v


@dataclass
class ScanResult:
    symbol: str
    filters: list[FilterResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(f.passed for f in self.filters)

    @property
    def failed(self) -> list[str]:
        return [f.id for f in self.filters if not f.passed]

    def reasons_json(self) -> dict[str, Any]:
        return {f.id: f.to_json() for f in self.filters}


def average_daily_volume(daily: Sequence[DailyVolume], lookback: int) -> tuple[int | None, int]:
    """Mean volume of the most recent ``lookback`` completed sessions → (adv, sessions used)."""
    recent = sorted(daily, key=lambda d: d.session_date)[-lookback:]
    if not recent:
        return None, 0
    return int(sum(d.volume for d in recent) / len(recent)), len(recent)


def evaluate(inputs: ScanInputs, cfg: UniverseSettings, *, feed: str, now: datetime,
             session: SessionDay | None) -> ScanResult:
    res = ScanResult(inputs.symbol)
    add = res.filters.append
    is_open = session is not None and session.open_at <= now < session.close_at
    last_bar = max(inputs.recent_bars, key=lambda b: b.ts) if inputs.recent_bars else None
    previous = max(inputs.daily, key=lambda d: d.session_date) if inputs.daily else None
    price = last_bar.close if last_bar else (previous.close if previous else None)

    # U1 enabled
    add(FilterResult("U1", "Enabled", inputs.enabled,
                     "enabled on the watchlist" if inputs.enabled else "disabled on the watchlist"))

    # U2 tradable-asset eligibility (CLAUDE.md "Tradable assets", E1-E7)
    elig = inputs.eligibility
    add(FilterResult("U2", "Eligible U.S. common stock", elig.eligible,
                     "passes E1-E7" if elig.eligible
                     else "fails " + ", ".join(f"{k} ({elig.rules[k].detail})"
                                               for k in elig.failed),
                     value=elig.failed or None))

    # U3 minimum price (never below the hard floor)
    floor = max(cfg.min_stock_price, HARD_MIN_STOCK_PRICE)
    if price is None:
        add(FilterResult("U3", "Minimum price", False, "no price data", limit=floor))
    else:
        add(FilterResult("U3", "Minimum price", price >= floor,
                         f"price ${price} vs minimum ${floor}", value=price, limit=floor))

    # U4 average daily volume, in units of the configured feed
    adv, used = average_daily_volume(inputs.daily, cfg.adv_lookback_days)
    needed = max(1, int(cfg.adv_lookback_days * MIN_HISTORY_FRACTION + 0.999))
    if adv is None or used < needed:
        add(FilterResult("U4", "Average daily volume", False,
                         f"insufficient history: {used} of {needed} required sessions "
                         f"({feed.upper()} daily bars) — run a backfill", value=adv,
                         limit=cfg.min_average_daily_volume))
    else:
        add(FilterResult("U4", "Average daily volume", adv >= cfg.min_average_daily_volume,
                         f"{used}-session ADV {adv:,} {feed.upper()} shares vs minimum "
                         f"{cfg.min_average_daily_volume:,}", value=adv,
                         limit=cfg.min_average_daily_volume))

    # U5 market cap (only when entered on the watchlist, D3)
    if inputs.market_cap is None:
        add(FilterResult("U5", "Market cap", True, "not entered on the watchlist: filter skipped",
                         limit=cfg.min_market_cap, evaluated=False))
    else:
        add(FilterResult("U5", "Market cap", inputs.market_cap >= cfg.min_market_cap,
                         f"${inputs.market_cap:,.0f} vs minimum ${cfg.min_market_cap:,.0f}",
                         value=inputs.market_cap, limit=cfg.min_market_cap))

    # U6 bid/ask spread (feed-specific threshold, G2)
    max_spread = Decimal(str(cfg.max_spread_percent_sip if feed == "sip"
                             else cfg.max_spread_percent_iex))
    q = inputs.quote
    if not is_open:
        add(FilterResult("U6", "Spread", True, "market closed: checked during the session "
                         "and again at order time", limit=max_spread, evaluated=False))
    elif q is None or now - q.ts > QUOTE_MAX_AGE:
        add(FilterResult("U6", "Spread", False,
                         "no fresh quote" if q is None
                         else f"quote is {int((now - q.ts).total_seconds())}s old",
                         limit=max_spread))
    elif q.spread_pct is None:
        add(FilterResult("U6", "Spread", False,
                         "crossed or one-sided quote" if not q.crossed else "crossed quote",
                         limit=max_spread))
    else:
        sp = q.spread_pct.quantize(Decimal("0.0001"))
        add(FilterResult("U6", "Spread", sp <= max_spread,
                         f"{sp}% vs maximum {max_spread}%", value=sp, limit=max_spread))

    # U7 bar coverage over the last 30 minutes (IEX: a missing bar means no data)
    if not is_open or session is None or now - session.open_at < COVERAGE_MIN_ELAPSED:
        add(FilterResult("U7", "Bar coverage", True, "too early in the session / market closed",
                         limit=cfg.min_bar_coverage, evaluated=False))
    else:
        cov = coverage([b.ts for b in inputs.recent_bars], session, now)
        add(FilterResult("U7", "Bar coverage", cov >= cfg.min_bar_coverage,
                         f"{cov:.0%} of recent minutes have a bar vs minimum "
                         f"{cfg.min_bar_coverage:.0%}", value=cov, limit=cfg.min_bar_coverage))

    # U8 data sanity (OHLC consistency, suspicious jump vs previous close)
    if last_bar is None:
        add(FilterResult("U8", "Data quality", True, "no bar today yet", evaluated=False))
    else:
        problems = bar_problems(last_bar, previous.close if previous else None)
        add(FilterResult("U8", "Data quality", not problems,
                         "; ".join(problems) if problems else "latest bar is consistent",
                         value=problems or None))

    # U9 trading activity: no bars for a while mid-session suggests a halt
    if not is_open or session is None or now - session.open_at < ACTIVITY_GRACE:
        add(FilterResult("U9", "Trading activity", True, "market closed / opening",
                         evaluated=False))
    else:
        age = (now - last_bar.end) if last_bar else None
        ok = age is not None and age <= NO_TRADES_SUSPECT
        add(FilterResult("U9", "Trading activity", ok,
                         "trading normally" if ok else
                         ("no bars today — possibly halted" if age is None
                          else f"no bar for {int(age.total_seconds() // 60)} min — "
                               "possibly halted"),
                         value=int(age.total_seconds()) if age is not None else None,
                         limit=int(NO_TRADES_SUSPECT.total_seconds())))

    change = ((price - previous.close) / previous.close * 100
              if price is not None and previous and previous.close > 0 else None)
    res.metrics = {
        "price": _j(price), "previous_close": _j(previous.close) if previous else None,
        "change_pct": float(round(change, 3)) if change is not None else None,
        "adv": adv, "adv_sessions": used,
        "spread_pct": _j(q.spread_pct.quantize(Decimal("0.0001")))
        if q is not None and q.spread_pct is not None else None,
        "last_bar_at": last_bar.ts.isoformat() if last_bar else None,
        "bars_today": len(inputs.recent_bars),
        "market_cap": _j(inputs.market_cap),
    }
    return res
