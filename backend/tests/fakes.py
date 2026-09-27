"""Test doubles: a fake Alpaca market-data WebSocket server (documented protocol)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

FIXTURES = Path(__file__).parent / "fixtures" / "alpaca"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeAlpacaStream:
    """Scriptable stand-in for wss://stream.data.alpaca.markets/v2/{feed}."""

    def __init__(self, *, key: str = "key", secret: str = "secret", symbol_limit: int = 30,
                 reject_subscribe_code: int | None = None) -> None:
        self.key, self.secret = key, secret
        self.symbol_limit = symbol_limit
        self.reject_subscribe_code = reject_subscribe_code
        self.connections = 0
        self.auth_attempts = 0
        self.subscribed: set[str] = set()
        self.actions: list[dict[str, Any]] = []
        self._clients: set[ServerConnection] = set()
        self._server: Server | None = None
        self.subscribed_event = asyncio.Event()

    @property
    def url(self) -> str:
        assert self._server is not None
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    async def __aenter__(self) -> FakeAlpacaStream:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _send(self, ws: ServerConnection, msgs: list[dict[str, Any]]) -> None:
        await ws.send(json.dumps(msgs))

    async def _handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        self._clients.add(ws)
        try:
            await self._send(ws, [{"T": "success", "msg": "connected"}])
            auth = json.loads(await ws.recv())
            self.auth_attempts += 1
            self.actions.append(auth)
            if auth.get("key") != self.key or auth.get("secret") != self.secret:
                await self._send(ws, [{"T": "error", "code": 402, "msg": "auth failed"}])
                await ws.close()
                return
            await self._send(ws, [{"T": "success", "msg": "authenticated"}])
            async for raw in ws:
                msg = json.loads(raw)
                self.actions.append(msg)
                bars = set(msg.get("bars", []))
                if msg.get("action") == "subscribe":
                    if self.reject_subscribe_code:
                        await self._send(ws, [{"T": "error", "code": self.reject_subscribe_code,
                                               "msg": "rejected"}])
                        continue
                    if len(self.subscribed | bars) > self.symbol_limit:
                        await self._send(ws, [{"T": "error", "code": 405,
                                               "msg": "symbol limit exceeded"}])
                        continue
                    self.subscribed |= bars
                elif msg.get("action") == "unsubscribe":
                    self.subscribed -= bars
                await self._send(ws, [{"T": "subscription", "trades": [], "quotes": [],
                                       "bars": sorted(self.subscribed),
                                       "updatedBars": sorted(self.subscribed),
                                       "dailyBars": [], "statuses": []}])
                self.subscribed_event.set()
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)

    async def push(self, msgs: list[dict[str, Any]]) -> None:
        for ws in list(self._clients):
            await self._send(ws, msgs)

    async def drop_all(self) -> None:
        self.subscribed_event.clear()
        for ws in list(self._clients):
            await ws.close()


def bar_msg(symbol: str, t: str, close: float, kind: str = "b") -> dict[str, Any]:
    return {"T": kind, "S": symbol, "o": close, "h": close + 0.1, "l": close - 0.1, "c": close,
            "v": 1000, "n": 10, "vw": close, "t": t}
