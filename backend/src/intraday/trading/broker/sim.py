"""SimBroker fill model (docs/design/08 §10). Pure functions over 1-minute bars.

Conservative by construction:
- An entry (marketable limit) fills on the *next* bar if its low reaches the
  limit, at min(limit, open) plus slippage, never above the limit.
- A stop fills at min(stop, open) minus slippage: gapping through the stop
  fills at the worse open price.
- A target fills at the target (a limit order).
- If one bar touches both stop and target, the stop is assumed first.
- On the bar an entry fills, only the stop is checked (the order of events
  inside that bar is unknown, so the unfavourable case is assumed).
- A market exit fills at the next bar's open minus slippage.

Phase 8 wraps these functions behind the Broker interface so replays go
through the real order manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from intraday.marketdata.types import Bar

BPS = Decimal("10000")
CENT = Decimal("0.0001")


def _q(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Fill:
    price: Decimal
    reason: str


def fill_entry(bar: Bar, limit: Decimal, slippage_bps: int) -> Fill | None:
    if bar.low > limit:
        return None
    base = min(limit, bar.open)
    return Fill(_q(min(limit, base * (1 + Decimal(slippage_bps) / BPS))), "ENTRY")


def check_bracket(bar: Bar, stop: Decimal, target: Decimal, slippage_bps: int, *,
                  entry_bar: bool = False) -> Fill | None:
    """Stop first (conservative); on the entry bar only the stop is considered."""
    if bar.low <= stop:
        base = min(stop, bar.open)
        return Fill(_q(base * (1 - Decimal(slippage_bps) / BPS)), "STOP_LOSS")
    if not entry_bar and bar.high >= target:
        return Fill(_q(max(target, bar.open) if bar.open > target else target), "TAKE_PROFIT")
    return None


def fill_market_exit(bar: Bar, slippage_bps: int) -> Decimal:
    return _q(bar.open * (1 - Decimal(slippage_bps) / BPS))
