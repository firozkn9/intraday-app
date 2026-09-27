"""Broker interface and broker-neutral models (docs/design/08 §1).

Implementations: AlpacaPaperBroker (paper API), SimBroker (replay/tests),
FutureLiveBroker (stub; raises). Everything above this layer — order manager,
position manager, reconciler — only sees these types.

Invariants every implementation keeps: long only, whole shares, entries are
bracket orders.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from intraday.marketdata.types import parse_ts

TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced",
                               "done_for_day", "stopped", "suspended"})


class BrokerError(Exception):
    def __init__(self, status: int, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"broker {status}: {message}")
        self.status, self.message, self.retryable = status, message, retryable


def _dec(v: Any) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


def _ts(v: Any) -> datetime | None:
    return None if not v else parse_ts(str(v))


@dataclass(frozen=True)
class BrokerOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str
    type: str
    order_class: str
    qty: int
    filled_qty: int
    filled_avg_price: Decimal | None
    status: str
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: str = "day"
    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    legs: tuple[BrokerOrder, ...] = ()
    replaces: str | None = None          # id of the order this one replaced (stop raises)
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @classmethod
    def from_alpaca(cls, d: dict[str, Any]) -> BrokerOrder:
        return cls(
            id=str(d["id"]), client_order_id=str(d.get("client_order_id") or ""),
            symbol=str(d.get("symbol") or ""), side=str(d.get("side") or ""),
            type=str(d.get("type") or d.get("order_type") or ""),
            order_class=str(d.get("order_class") or "simple") or "simple",
            qty=int(Decimal(str(d.get("qty") or 0))),
            filled_qty=int(Decimal(str(d.get("filled_qty") or 0))),
            filled_avg_price=_dec(d.get("filled_avg_price")), status=str(d.get("status") or ""),
            limit_price=_dec(d.get("limit_price")), stop_price=_dec(d.get("stop_price")),
            time_in_force=str(d.get("time_in_force") or "day"),
            submitted_at=_ts(d.get("submitted_at")), updated_at=_ts(d.get("updated_at")),
            legs=tuple(cls.from_alpaca(x) for x in (d.get("legs") or [])),
            replaces=str(d["replaces"]) if d.get("replaces") else None, raw=d)


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: int
    avg_entry_price: Decimal
    current_price: Decimal | None = None

    @classmethod
    def from_alpaca(cls, d: dict[str, Any]) -> BrokerPosition:
        return cls(str(d["symbol"]), int(Decimal(str(d.get("qty") or 0))),
                   Decimal(str(d.get("avg_entry_price") or 0)), _dec(d.get("current_price")))


@dataclass(frozen=True)
class BrokerAccount:
    account_number: str
    status: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    last_equity: Decimal
    long_market_value: Decimal
    trading_blocked: bool
    shorting_enabled: bool
    multiplier: str

    @classmethod
    def from_alpaca(cls, d: dict[str, Any]) -> BrokerAccount:
        z = Decimal(0)
        return cls(str(d.get("account_number") or ""), str(d.get("status") or ""),
                   _dec(d.get("equity")) or z, _dec(d.get("cash")) or z,
                   _dec(d.get("buying_power")) or z, _dec(d.get("last_equity")) or z,
                   _dec(d.get("long_market_value")) or z, bool(d.get("trading_blocked")),
                   bool(d.get("shorting_enabled")), str(d.get("multiplier") or ""))


@dataclass(frozen=True)
class TradeUpdate:
    event: str
    order: BrokerOrder
    timestamp: datetime | None
    price: Decimal | None = None
    qty: int | None = None
    position_qty: int | None = None
    execution_id: str | None = None

    @classmethod
    def from_alpaca(cls, d: dict[str, Any]) -> TradeUpdate:
        return cls(str(d["event"]), BrokerOrder.from_alpaca(d["order"]), _ts(d.get("timestamp")),
                   _dec(d.get("price")),
                   int(Decimal(str(d["qty"]))) if d.get("qty") not in (None, "") else None,
                   int(Decimal(str(d["position_qty"])))
                   if d.get("position_qty") not in (None, "") else None,
                   str(d["execution_id"]) if d.get("execution_id") else None)


@dataclass(frozen=True)
class BracketEntry:
    client_order_id: str
    symbol: str
    qty: int
    limit_price: Decimal
    take_profit_price: Decimal
    stop_loss_price: Decimal


@dataclass(frozen=True)
class MarketExit:
    client_order_id: str
    symbol: str
    qty: int


@dataclass(frozen=True)
class ProtectiveOco:
    """Exit-only OCO (take-profit + stop) re-protecting an existing long position."""

    client_order_id: str
    symbol: str
    qty: int
    take_profit_price: Decimal
    stop_price: Decimal


TradeUpdateHandler = Callable[[TradeUpdate], Awaitable[None]]


class Broker(ABC):
    name: str

    @abstractmethod
    async def get_account(self) -> BrokerAccount: ...

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def get_open_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    async def get_orders_since(self, after: datetime) -> list[BrokerOrder]: ...

    @abstractmethod
    async def get_order(self, broker_order_id: str) -> BrokerOrder | None: ...

    @abstractmethod
    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    @abstractmethod
    async def submit_bracket_entry(self, order: BracketEntry) -> BrokerOrder: ...

    @abstractmethod
    async def submit_market_exit(self, order: MarketExit) -> BrokerOrder: ...

    @abstractmethod
    async def submit_protective_oco(self, order: ProtectiveOco) -> BrokerOrder: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    async def replace_stop(self, broker_order_id: str, stop_price: Decimal) -> BrokerOrder: ...

    @abstractmethod
    async def run_trade_updates(self, stop: asyncio.Event, handler: TradeUpdateHandler
                                ) -> None: ...

    @abstractmethod
    async def configure_account(self) -> dict[str, Any]: ...
