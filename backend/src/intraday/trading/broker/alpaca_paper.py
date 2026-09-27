"""AlpacaPaperBroker: the only real broker in this project (paper API only).

REST (docs.alpaca.markets, checked 2026-09-26):
  GET    /v2/account                       PATCH /v2/account/configurations
  GET    /v2/positions
  GET    /v2/orders?status=open&nested=true    GET /v2/orders/{id}?nested=true
  GET    /v2/orders:by_client_order_id?client_order_id=…
  POST   /v2/orders   (bracket: order_class=bracket, take_profit{limit_price},
                        stop_loss{stop_price})
  PATCH  /v2/orders/{id}   (replace; used for the stop leg)
  DELETE /v2/orders/{id}
Trade updates: wss://paper-api.alpaca.markets/stream (binary frames), auth then
{"action":"listen","data":{"streams":["trade_updates"]}}.

Order submissions are never retried blindly (see OrderManager): the HTTP layer
is called with retries disabled for writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from intraday.core.logging import get_logger
from intraday.core.safety import assert_paper_mode
from intraday.marketdata.alpaca.http import AlpacaError, AlpacaHttp, Credentials, Sleep
from intraday.trading.broker.base import (
    BracketEntry,
    Broker,
    BrokerAccount,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    MarketExit,
    ProtectiveOco,
    TradeUpdate,
    TradeUpdateHandler,
)

log = get_logger(__name__)

TRADE_STREAM_URL = "wss://paper-api.alpaca.markets/stream"
LONG_ONLY_CONFIG = {"no_shorting": True, "fractional_trading": False,
                    "max_margin_multiplier": "1"}


def _money(x: Decimal) -> str:
    return str(x.quantize(Decimal("0.01")))


def _err(exc: AlpacaError) -> BrokerError:
    return BrokerError(exc.status, str(exc), retryable=exc.status in (0, 429, 500, 502, 503, 504))


class AlpacaPaperBroker(Broker):
    name = "alpaca_paper"

    def __init__(self, http: AlpacaHttp, credentials: Credentials, *, trading_mode: str,
                 stream_url: str = TRADE_STREAM_URL, sleep: Sleep = asyncio.sleep,
                 on_stream_state: Callable[[str], Awaitable[None]] | None = None) -> None:
        # Guard twice: here, and in the factory (docs/design/08 §2).
        assert_paper_mode(trading_mode, http.base_url)
        self.http = http
        self._creds = credentials
        self.stream_url = stream_url
        self._sleep = sleep
        self.on_stream_state = on_stream_state
        self.stream_state = "idle"

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _call(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await fn()
        except AlpacaError as exc:
            raise _err(exc) from exc

    # ------------------------------------------------------------------ reads

    async def get_account(self) -> BrokerAccount:
        return BrokerAccount.from_alpaca(await self._call(lambda: self.http.get("/v2/account")))

    async def get_positions(self) -> list[BrokerPosition]:
        rows = await self._call(lambda: self.http.get("/v2/positions"))
        return [BrokerPosition.from_alpaca(r) for r in rows or []]

    async def get_open_orders(self) -> list[BrokerOrder]:
        rows = await self._call(lambda: self.http.get(
            "/v2/orders", {"status": "open", "nested": "true", "limit": 500}))
        return [BrokerOrder.from_alpaca(r) for r in rows or []]

    async def get_orders_since(self, after: datetime) -> list[BrokerOrder]:
        rows = await self._call(lambda: self.http.get(
            "/v2/orders", {"status": "all", "nested": "true", "limit": 500,
                           "after": after.isoformat().replace("+00:00", "Z")}))
        return [BrokerOrder.from_alpaca(r) for r in rows or []]

    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        try:
            raw = await self.http.get(f"/v2/orders/{broker_order_id}", {"nested": "true"})
        except AlpacaError as exc:
            if exc.status == 404:
                return None
            raise _err(exc) from exc
        return BrokerOrder.from_alpaca(raw)

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        try:
            raw = await self.http.get("/v2/orders:by_client_order_id",
                                      {"client_order_id": client_order_id})
        except AlpacaError as exc:
            if exc.status in (404, 422):
                return None
            raise _err(exc) from exc
        return BrokerOrder.from_alpaca(raw)

    # ------------------------------------------------------------------ writes

    async def submit_bracket_entry(self, order: BracketEntry) -> BrokerOrder:
        body = {"symbol": order.symbol, "qty": str(order.qty), "side": "buy", "type": "limit",
                "time_in_force": "day", "limit_price": _money(order.limit_price),
                "order_class": "bracket", "client_order_id": order.client_order_id,
                "take_profit": {"limit_price": _money(order.take_profit_price)},
                "stop_loss": {"stop_price": _money(order.stop_loss_price)}}
        return BrokerOrder.from_alpaca(await self._call(lambda: self.http.post("/v2/orders",
                                                                               body)))

    async def submit_market_exit(self, order: MarketExit) -> BrokerOrder:
        body = {"symbol": order.symbol, "qty": str(order.qty), "side": "sell",
                "type": "market", "time_in_force": "day",
                "client_order_id": order.client_order_id}
        return BrokerOrder.from_alpaca(await self._call(lambda: self.http.post("/v2/orders",
                                                                               body)))

    async def submit_protective_oco(self, order: ProtectiveOco) -> BrokerOrder:
        body = {"symbol": order.symbol, "qty": str(order.qty), "side": "sell", "type": "limit",
                "time_in_force": "day", "order_class": "oco",
                "client_order_id": order.client_order_id,
                "take_profit": {"limit_price": _money(order.take_profit_price)},
                "stop_loss": {"stop_price": _money(order.stop_price)}}
        return BrokerOrder.from_alpaca(await self._call(lambda: self.http.post("/v2/orders",
                                                                               body)))

    async def cancel_order(self, broker_order_id: str) -> None:
        await self._call(lambda: self.http.delete(f"/v2/orders/{broker_order_id}", retry=False))

    async def replace_stop(self, broker_order_id: str, stop_price: Decimal) -> BrokerOrder:
        return BrokerOrder.from_alpaca(await self._call(lambda: self.http.patch(
            f"/v2/orders/{broker_order_id}", {"stop_price": _money(stop_price)})))

    async def configure_account(self) -> dict[str, Any]:
        """Long-only, whole-share, no-margin account settings; read back and verified."""
        result: dict[str, Any] = await self._call(lambda: self.http.patch(
            "/v2/account/configurations", LONG_ONLY_CONFIG, retry=True))
        mismatched = {k: result.get(k) for k, v in LONG_ONLY_CONFIG.items()
                      if result.get(k) != v}
        if mismatched:
            raise BrokerError(0, f"account configuration not applied: {mismatched}")
        return result

    # ------------------------------------------------------------------ trade updates

    async def run_trade_updates(self, stop: asyncio.Event, handler: TradeUpdateHandler) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                await self._stream_session(stop, handler)
                backoff = 1.0
            except BrokerError as exc:
                log.error("trade_stream_error", error=str(exc))
                backoff = 60.0
            except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError,
                    json.JSONDecodeError) as exc:
                log.warning("trade_stream_disconnected", error=f"{type(exc).__name__}: {exc}")
            await self._set_state("disconnected")
            if stop.is_set():
                break
            delay = backoff * (0.8 + 0.4 * random.random())  # noqa: S311 - jitter
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            backoff = min(backoff * 2, 60.0)
        await self._set_state("stopped")

    async def _set_state(self, state: str) -> None:
        self.stream_state = state
        if self.on_stream_state is not None:
            await self.on_stream_state(state)

    async def _recv(self, ws: Any, wait_s: float | None = None) -> dict[str, Any]:
        raw = await (asyncio.wait_for(ws.recv(), wait_s) if wait_s else ws.recv())
        text = raw.decode() if isinstance(raw, bytes) else raw
        msg: dict[str, Any] = json.loads(text)
        return msg

    async def _stream_session(self, stop: asyncio.Event, handler: TradeUpdateHandler) -> None:
        self.stream_state = "connecting"
        async with ws_connect(self.stream_url, ping_interval=20, ping_timeout=20,
                              open_timeout=10) as ws:
            await ws.send(json.dumps({"action": "auth", "key": self._creds.key_id,
                                      "secret": self._creds.secret_key}))
            auth = await self._recv(ws, 10)
            if (auth.get("stream") != "authorization"
                    or (auth.get("data") or {}).get("status") != "authorized"):
                raise BrokerError(401, f"trade stream auth failed: {auth.get('data')}")
            await ws.send(json.dumps({"action": "listen",
                                      "data": {"streams": ["trade_updates"]}}))
            ack = await self._recv(ws, 10)
            if ack.get("stream") != "listening":
                raise BrokerError(0, f"trade stream listen failed: {ack}")
            await self._set_state("connected")
            log.info("trade_stream_connected")
            stop_task = asyncio.ensure_future(stop.wait())
            try:
                while not stop.is_set():
                    recv = asyncio.ensure_future(self._recv(ws))
                    done, _ = await asyncio.wait({recv, stop_task},
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if recv not in done:
                        recv.cancel()
                        return
                    msg = recv.result()
                    if msg.get("stream") == "trade_updates":
                        await handler(TradeUpdate.from_alpaca(msg["data"]))
            finally:
                stop_task.cancel()
