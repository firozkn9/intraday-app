"""Watchlist rules shared by the API and the engine: stream budget, eligibility, seed.

Stream budget (free plan: 30 symbols). Subscription priority:
  1. core CONTEXT symbols (no sector: SPY, QQQ, volatility proxy),
  2. sector-ETF CONTEXT symbols referenced by an enabled TRADABLE symbol,
  3. enabled TRADABLE symbols whose hand-entered rules (E1-E3, E6) pass.
Within a tier, older entries come first, so the newest additions are the ones
dropped when the budget is exceeded, and they're reported.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from intraday.core.assets import AlpacaAsset, Eligibility, SymbolMeta, evaluate, manual_rules


class SymbolLike(Protocol):
    symbol: str
    role: str
    asset_type: str
    country: str | None
    sector: str | None
    sector_etf: str | None
    enabled: bool
    alpaca_class: str | None
    alpaca_exchange: str | None
    alpaca_name: str | None
    alpaca_tradable: bool | None
    alpaca_status: str | None
    alpaca_synced_at: datetime | None


_EPOCH = datetime.min.replace(tzinfo=UTC)


def _added_at(row: SymbolLike) -> datetime:
    return getattr(row, "created_at", None) or _EPOCH


def meta_of(row: SymbolLike) -> SymbolMeta:
    return SymbolMeta(row.symbol, row.role, row.asset_type, row.country)


def asset_of(row: SymbolLike) -> AlpacaAsset | None:
    if row.alpaca_synced_at is None:
        return None
    return AlpacaAsset(row.symbol, row.alpaca_name or "", row.alpaca_class or "",
                       row.alpaca_exchange or "", row.alpaca_status or "",
                       bool(row.alpaca_tradable), row.alpaca_synced_at)


def eligibility_of(row: SymbolLike, now: datetime) -> Eligibility:
    return evaluate(meta_of(row), asset_of(row), now)


def manually_plausible(row: SymbolLike) -> bool:
    return all(r.passed for r in manual_rules(meta_of(row)).values())


@dataclass(frozen=True)
class StreamPlan:
    subscribed: list[str]
    dropped: list[str]
    limit: int

    def to_json(self) -> dict[str, Any]:
        return {"used": len(self.subscribed), "limit": self.limit,
                "subscribed": self.subscribed, "dropped": self.dropped}


def stream_plan(rows: Iterable[SymbolLike], limit: int) -> StreamPlan:
    enabled = [r for r in rows if r.enabled]
    # Oldest first: adding a symbol never pushes an existing one out of the stream.
    tradable = sorted((r for r in enabled if r.role == "TRADABLE" and manually_plausible(r)),
                      key=lambda r: (_added_at(r), r.symbol))
    referenced = {r.sector_etf for r in tradable if r.sector_etf}
    first = {"SPY": 0, "QQQ": 1}  # the regime detector's reference symbols
    core = sorted((r.symbol for r in enabled if r.role == "CONTEXT" and not r.sector),
                  key=lambda s: (first.get(s, 2), s))
    sectors = sorted(r.symbol for r in enabled
                     if r.role == "CONTEXT" and r.sector and r.symbol in referenced)
    ordered = core + sectors + [r.symbol for r in tradable]
    return StreamPlan(ordered[:limit], ordered[limit:], limit)


# ------------------------------------------------------------------ seed file

@dataclass(frozen=True)
class SeedEntry:
    symbol: str
    role: str
    asset_type: str
    country: str | None
    name: str | None = None
    sector: str | None = None
    sector_etf: str | None = None
    market_cap: int | None = None
    notes: str | None = None
    enabled: bool = True

    def to_row(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "symbol", "role", "asset_type", "country", "name", "sector", "sector_etf",
            "market_cap", "notes", "enabled")}


@dataclass
class Seed:
    context: list[SeedEntry] = field(default_factory=list)
    tradable: list[SeedEntry] = field(default_factory=list)

    @property
    def all(self) -> list[SeedEntry]:
        return self.context + self.tradable


def load_seed(path: Path) -> Seed:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    seed = Seed()
    for item in data.get("context", []):
        seed.context.append(SeedEntry(role="CONTEXT", asset_type=item.pop("asset_type", "ETF"),
                                      country=item.pop("country", "US"), **item))
    for item in data.get("tradable", []):
        seed.tradable.append(SeedEntry(role="TRADABLE", asset_type="COMMON_STOCK",
                                       country=item.pop("country", "US"), **item))
    return seed


def validate_seed(seed: Seed, stream_limit: int) -> list[str]:
    """Problems that would make the seed unsafe or unusable."""
    problems = []
    for e in seed.tradable:
        failed = [k for k, r in manual_rules(SymbolMeta(e.symbol, e.role, e.asset_type,
                                                        e.country)).items() if not r.passed]
        if failed:
            problems.append(f"{e.symbol}: fails {', '.join(failed)}")
    context = {e.symbol for e in seed.context}
    for e in seed.tradable:
        if e.sector_etf and e.sector_etf not in context:
            problems.append(f"{e.symbol}: sector ETF {e.sector_etf} is not a context symbol")
    plan = stream_plan(_SeedRows(seed.all), stream_limit)
    if plan.dropped:
        problems.append(f"stream budget exceeded; dropped {', '.join(plan.dropped)}")
    return problems


class _SeedRows(list):  # type: ignore[type-arg]
    """Adapts seed entries to SymbolLike for stream planning."""

    def __init__(self, entries: Sequence[SeedEntry]) -> None:
        super().__init__(_Row(e) for e in entries)


class _Row:
    def __init__(self, e: SeedEntry) -> None:
        self.symbol, self.role = e.symbol, e.role
        self.asset_type, self.country = e.asset_type, e.country
        self.sector, self.sector_etf, self.enabled = e.sector, e.sector_etf, e.enabled
        self.alpaca_class = self.alpaca_exchange = self.alpaca_name = self.alpaca_status = None
        self.alpaca_tradable = None
        self.alpaca_synced_at = None
