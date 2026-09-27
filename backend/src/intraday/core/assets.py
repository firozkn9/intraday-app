"""Tradable-asset eligibility (CLAUDE.md "Tradable assets"; docs/design/07 §2a).

One pure function, applied independently by the universe filter, the risk
engine (R26), and the order manager. A symbol is eligible only if EVERY rule
passes. Heuristics (E6, E7) can only reject, never admit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

TRADABLE_EXCHANGES = frozenset({"NYSE", "NASDAQ"})
ASSET_CACHE_MAX_AGE = timedelta(days=7)
SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,5}(\.[A-B])?$")
DISQUALIFYING_NAME = re.compile(
    r"\b(ETFs?|ETNs?|Fund|Index|Warrants?|Rights?|Units?|Preferred|Pfd|Depositary|ADRs?|ADSs?|"
    r"Notes?|Debentures?|Bonds?|Leveraged|Inverse|[23]x)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SymbolMeta:
    """Hand-entered watchlist fields (D3)."""

    symbol: str
    role: str  # TRADABLE | CONTEXT
    asset_type: str  # COMMON_STOCK | ETF | OTHER
    country: str | None


@dataclass(frozen=True)
class AlpacaAsset:
    """Cached subset of Alpaca's /v2/assets/{symbol} response."""

    symbol: str
    name: str
    asset_class: str
    exchange: str
    status: str
    tradable: bool
    synced_at: datetime

    @classmethod
    def from_api(cls, data: dict[str, Any], synced_at: datetime) -> AlpacaAsset:
        return cls(symbol=str(data["symbol"]), name=str(data.get("name") or ""),
                   asset_class=str(data.get("class") or ""),
                   exchange=str(data.get("exchange") or ""),
                   status=str(data.get("status") or ""), tradable=bool(data.get("tradable")),
                   synced_at=synced_at)


@dataclass(frozen=True)
class RuleResult:
    passed: bool
    detail: str


@dataclass(frozen=True)
class Eligibility:
    rules: dict[str, RuleResult] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return bool(self.rules) and all(r.passed for r in self.rules.values())

    @property
    def failed(self) -> list[str]:
        return [k for k, r in self.rules.items() if not r.passed]

    def to_json(self) -> dict[str, Any]:
        return {"eligible": self.eligible,
                "rules": {k: {"passed": r.passed, "detail": r.detail}
                          for k, r in self.rules.items()}}


def manual_rules(meta: SymbolMeta) -> dict[str, RuleResult]:
    """Rules checkable from hand-entered data alone (E1-E3, E6)."""
    return {
        "E1": RuleResult(meta.role == "TRADABLE",
                         "role is TRADABLE" if meta.role == "TRADABLE"
                         else f"role is {meta.role}: market-context input, never tradable"),
        "E2": RuleResult(meta.asset_type == "COMMON_STOCK",
                         f"asset type is {meta.asset_type}"),
        "E3": RuleResult(meta.country == "US", f"issuer country is {meta.country or 'unknown'}"),
        "E6": RuleResult(bool(SYMBOL_PATTERN.match(meta.symbol)),
                         "plain common-stock ticker" if SYMBOL_PATTERN.match(meta.symbol)
                         else "ticker suffix suggests preferred/warrant/unit/right"),
    }


def evaluate(meta: SymbolMeta, asset: AlpacaAsset | None, now: datetime) -> Eligibility:
    rules = manual_rules(meta)
    if asset is None:
        missing = RuleResult(False, "Alpaca asset data not synced yet")
        rules.update({"E4": missing, "E5": missing, "E7": missing})
    elif now - asset.synced_at > ASSET_CACHE_MAX_AGE:
        stale = RuleResult(False, f"Alpaca asset data is stale (synced {asset.synced_at:%Y-%m-%d})")
        rules.update({"E4": stale, "E5": stale, "E7": stale})
    else:
        e4 = asset.asset_class == "us_equity" and asset.exchange in TRADABLE_EXCHANGES
        rules["E4"] = RuleResult(e4, f"class {asset.asset_class}, exchange {asset.exchange}")
        e5 = asset.status == "active" and asset.tradable
        rules["E5"] = RuleResult(e5, f"status {asset.status}, tradable={asset.tradable}")
        hit = DISQUALIFYING_NAME.search(asset.name)
        rules["E7"] = RuleResult(hit is None, "name has no disqualifying terms" if hit is None
                                 else f"name contains '{hit.group(0)}'")
    return Eligibility(dict(sorted(rules.items())))


def is_eligible_common_stock(meta: SymbolMeta, asset: AlpacaAsset | None, now: datetime) -> bool:
    return evaluate(meta, asset, now).eligible
