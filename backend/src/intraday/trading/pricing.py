"""Entry pricing and client order IDs (docs/design/08 §4, §6). Pure.

The entry is a marketable limit: ``min(ask, reference) x (1 + offset)``, never
above the signal bar's close plus the offset. Stop and target keep their
distance from the entry, so the risk engine sizes exactly what is submitted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
BPS = Decimal("10000")

INTENT_CODES = {"ENTRY": "EN", "ENGINE_EXIT": "EX", "FORCE_CLOSE": "FC",
                "EMERGENCY_CLOSE": "EM", "MANUAL_CLOSE": "MC", "PROTECT": "PR"}


def cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class EntryPrices:
    limit: Decimal
    stop: Decimal
    target: Decimal


def price_entry(reference: Decimal, stop: Decimal, target: Decimal, ask: Decimal | None,
                offset_bps: int) -> EntryPrices:
    factor = 1 + Decimal(offset_bps) / BPS
    base = min(ask, reference) if ask is not None and ask > 0 else reference
    limit = cents(base * factor)
    shift = limit - reference
    new_stop = min(cents(stop + shift), limit - CENT)
    new_target = max(cents(target + shift), limit + CENT)
    return EntryPrices(limit, new_stop, new_target)


def short_id(ref: uuid.UUID | str) -> str:
    """Last 8 hex digits: the random part of a UUIDv7."""
    return str(ref).replace("-", "")[-8:]


def client_order_id(day: date, intent: str, ref: uuid.UUID | str, attempt: int) -> str:
    """``ia-{yyyymmdd}-{intent}-{shortid}-{attempt}``, unique per logical order."""
    return f"ia-{day:%Y%m%d}-{INTENT_CODES[intent]}-{short_id(ref)}-{attempt}"
