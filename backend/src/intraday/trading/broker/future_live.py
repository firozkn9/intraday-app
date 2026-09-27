"""FutureLiveBroker: interface stub only. Live trading is NOT implemented.

CLAUDE.md: "FutureLiveBroker is an interface stub that raises
NotImplementedError. Never write live-order code." Any future live broker is
a separate project with its own safety review (SPEC §49).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, NoReturn

from intraday.trading.broker.base import (
    BracketEntry,
    Broker,
    BrokerAccount,
    BrokerOrder,
    BrokerPosition,
    MarketExit,
    ProtectiveOco,
    TradeUpdateHandler,
)

_MESSAGE = "Live trading is not implemented. This project is paper trading only."


def _refuse() -> NoReturn:
    raise NotImplementedError(_MESSAGE)


class FutureLiveBroker(Broker):
    name = "future_live"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _refuse()

    async def get_account(self) -> BrokerAccount:
        _refuse()

    async def get_positions(self) -> list[BrokerPosition]:
        _refuse()

    async def get_open_orders(self) -> list[BrokerOrder]:
        _refuse()

    async def get_orders_since(self, after: datetime) -> list[BrokerOrder]:
        _refuse()

    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        _refuse()

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        _refuse()

    async def submit_bracket_entry(self, order: BracketEntry) -> BrokerOrder:
        _refuse()

    async def submit_market_exit(self, order: MarketExit) -> BrokerOrder:
        _refuse()

    async def submit_protective_oco(self, order: ProtectiveOco) -> BrokerOrder:
        _refuse()

    async def cancel_order(self, broker_order_id: str) -> None:
        _refuse()

    async def replace_stop(self, broker_order_id: str, stop_price: Decimal) -> BrokerOrder:
        _refuse()

    async def run_trade_updates(self, stop: asyncio.Event, handler: TradeUpdateHandler) -> None:
        _refuse()

    async def configure_account(self) -> dict[str, Any]:
        _refuse()
