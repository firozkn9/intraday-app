"""Risk-engine inputs and outputs (docs/design/07 §1). Plain immutable snapshots.

The risk engine is a pure function of these: no I/O, no clock reads, no
randomness. Money is Decimal throughout.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from intraday.core.assets import Eligibility
from intraday.features.regime import Regime
from intraday.marketdata.calendar import EntryWindow

ZERO = Decimal("0")


def jsonable(v: Any) -> Any:
    """Decimals → strings, recursively (JSONB can't store Decimal)."""
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    return v


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    qty: int
    avg_entry: Decimal
    mark: Decimal
    stop: Decimal
    sector: str | None = None

    @property
    def value(self) -> Decimal:
        return self.mark * self.qty

    @property
    def open_risk(self) -> Decimal:
        """Loss if the current stop is hit from the current mark (0 once stop ≥ mark)."""
        return max(ZERO, (self.mark - self.stop) * self.qty)

    @property
    def unrealized(self) -> Decimal:
        return (self.mark - self.avg_entry) * self.qty


@dataclass(frozen=True)
class PendingEntry:
    symbol: str
    qty: int
    limit_price: Decimal
    stop: Decimal
    sector: str | None = None

    @property
    def value(self) -> Decimal:
        return self.limit_price * self.qty

    @property
    def risk(self) -> Decimal:
        return max(ZERO, (self.limit_price - self.stop) * self.qty)


@dataclass(frozen=True)
class PortfolioSnapshot:
    positions: tuple[OpenPosition, ...] = ()
    pending: tuple[PendingEntry, ...] = ()
    realized_pnl_today: Decimal = ZERO      # net of fees
    trades_today: int = 0                   # entries submitted today (incl. pending)
    symbol_trades_today: Mapping[str, int] = field(default_factory=dict)
    last_exit_at: Mapping[str, datetime] = field(default_factory=dict)

    @property
    def exposure(self) -> Decimal:
        return sum((p.value for p in self.positions), ZERO)

    @property
    def pending_exposure(self) -> Decimal:
        return sum((p.value for p in self.pending), ZERO)

    @property
    def unrealized(self) -> Decimal:
        return sum((p.unrealized for p in self.positions), ZERO)

    @property
    def daily_pnl(self) -> Decimal:
        return self.realized_pnl_today + self.unrealized

    @property
    def open_risk(self) -> Decimal:
        return (sum((p.open_risk for p in self.positions), ZERO)
                + sum((p.risk for p in self.pending), ZERO))

    def holds(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.positions)

    def pending_for(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.pending)

    def sector_exposure(self, sector: str | None, symbol: str) -> Decimal:
        key = sector or f"symbol:{symbol}"
        return (sum((p.value for p in self.positions if (p.sector or f"symbol:{p.symbol}") == key),
                    ZERO)
                + sum((p.value for p in self.pending
                       if (p.sector or f"symbol:{p.symbol}") == key), ZERO))


@dataclass(frozen=True)
class AccountSnapshot:
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    source: Literal["BROKER", "DERIVED"] = "DERIVED"


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    last_price: Decimal
    spread_pct: Decimal | None          # None: no fresh two-sided quote
    data_problems: tuple[str, ...]      # empty = data OK
    eligibility: Eligibility
    sector: str | None
    blocked_news_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateSnapshot:
    blocks: tuple[str, ...]
    paper_mode_ok: bool


@dataclass(frozen=True)
class Clock:
    now: datetime
    market_open: bool
    window: EntryWindow | None


@dataclass(frozen=True)
class RiskProposal:
    symbol: str
    side: Literal["BUY", "SELL"]
    entry: Decimal
    stop: Decimal
    target: Decimal
    ai_mode: str = "OFF"
    llm_verdict: str | None = None


@dataclass(frozen=True)
class CheckResult:
    id: str
    name: str
    passed: bool
    detail: str
    value: Any = None
    limit: Any = None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "passed": self.passed, "detail": self.detail,
                "value": jsonable(self.value), "limit": jsonable(self.limit)}


@dataclass(frozen=True)
class Sizing:
    qty: int
    bounds: Mapping[str, int]
    binding: str | None
    risk_per_share: Decimal
    multiplier: Decimal
    effective_limits: Mapping[str, Decimal]

    def to_json(self) -> dict[str, Any]:
        return {"qty": self.qty, "bounds": dict(self.bounds), "binding": self.binding,
                "risk_per_share": str(self.risk_per_share), "multiplier": str(self.multiplier),
                "effective_limits": {k: str(v) for k, v in self.effective_limits.items()}}


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    checks: tuple[CheckResult, ...]
    sizing: Sizing
    qty: int
    position_value: Decimal
    max_loss: Decimal
    exposure_after: Decimal
    open_positions_after: int
    daily_loss_worst_case_after: Decimal
    risk_reward: Decimal | None
    error: str | None = None
    proposal: RiskProposal | None = None   # as evaluated (priced entry, stop, target)

    @property
    def failed(self) -> list[str]:
        return [c.id for c in self.checks if not c.passed]

    @property
    def only_gate_blocked(self) -> bool:
        return self.failed == ["R1"]

    def checks_json(self) -> list[dict[str, Any]]:
        return [c.to_json() for c in self.checks]


@dataclass(frozen=True)
class RiskInputs:
    proposal: RiskProposal
    portfolio: PortfolioSnapshot
    account: AccountSnapshot
    market: MarketSnapshot
    gate: GateSnapshot
    clock: Clock
    regime: Regime
    feed: str
    correlated_exposure: Decimal = ZERO   # exposure of held symbols in the candidate's cluster
