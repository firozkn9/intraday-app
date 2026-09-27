"""Alpaca real-time bar stream (docs.alpaca.markets "Streaming Market Data").

Protocol (JSON arrays): server → [{"T":"success","msg":"connected"}]; client →
{"action":"auth","key","secret"}; server → [{"T":"success","msg":"authenticated"}];
client → {"action":"subscribe","bars":[…],"updatedBars":[…]}; server →
[{"T":"subscription",…}]; then [{"T":"b"|"u","S","o","h","l","c","v","vw","n","t"}, …].
Errors: [{"T":"error","code":int,"msg":str}]; 10 s to authenticate.

Free plan: 30 symbol subscriptions (verified 2026-09-26). Reconnects with
exponential backoff + jitter; after every reconnect the caller gap-fills via REST.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from intraday.core.logging import get_logger
from intraday.core.timeutil import utcnow
from intraday.marketdata.alpaca.http import Credentials, Sleep
from intraday.marketdata.types import Bar

log = get_logger(__name__)

STREAM_BASE_URL = "wss://stream.data.alpaca.markets/v2"
FREE_PLAN_SYMBOL_LIMIT = 30
AUTH_TIMEOUT_SECONDS = 10.0

# Codes after which reconnecting is pointless until configuration changes.
FATAL_CODES = {402: "auth failed", 405: "symbol limit exceeded",
               409: "insufficient subscription", 410: "invalid subscribe action for this feed"}
# Another session holds our connection slot: back off to the maximum.
SLOW_BACKOFF_CODES = {406: "connection limit exceeded"}

State = Literal["idle", "connecting", "connected", "backoff", "fatal", "stopped"]


class StreamError(Exception):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(f"stream error {code}: {msg}")
        self.code, self.msg = code, msg


@dataclass(frozen=True)
class StreamEvent:
    kind: Literal["bar", "updated_bar", "status", "connected", "disconnected", "fatal"]
    bar: Bar | None = None
    raw: dict[str, Any] | None = None
    detail: str | None = None


Handler = Callable[[StreamEvent], Awaitable[None]]


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[WebSocketLike]]


async def _default_connect(url: str) -> WebSocketLike:
    return await ws_connect(url, ping_interval=20, ping_timeout=20, open_timeout=10,
                            max_size=2 ** 22)


@dataclass
class StreamStats:
    state: State = "idle"
    connected_since: datetime | None = None
    last_message_at: datetime | None = None
    reconnects: int = 0
    last_error: str | None = None
    subscribed: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        def iso(ts: datetime | None) -> str | None:
            return ts.isoformat() if ts else None

        return {"state": self.state, "connected_since": iso(self.connected_since),
                "last_message_at": iso(self.last_message_at), "reconnects": self.reconnects,
                "last_error": self.last_error, "subscribed": len(self.subscribed)}


class AlpacaBarStream:
    def __init__(self, credentials: Credentials, feed: str, *, url: str | None = None,
                 connect: Connector = _default_connect, sleep: Sleep = asyncio.sleep,
                 backoff_initial: float = 1.0, backoff_max: float = 60.0,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self._creds = credentials
        self.feed = feed
        self.url = url or f"{STREAM_BASE_URL}/{feed}"
        self._connect = connect
        self._sleep = sleep
        self._backoff_initial, self._backoff_max = backoff_initial, backoff_max
        self._clock = clock
        self._desired: list[str] = []
        self._ws: WebSocketLike | None = None
        self._lock = asyncio.Lock()
        self.stats = StreamStats()

    # ------------------------------------------------------------------ public

    async def set_symbols(self, symbols: Iterable[str]) -> None:
        """Change the subscription; applied immediately if connected."""
        desired = sorted(set(symbols))
        async with self._lock:
            current = set(self.stats.subscribed)
            self._desired = desired
            ws = self._ws
            if ws is None or self.stats.state != "connected":
                return
            add, remove = sorted(set(desired) - current), sorted(current - set(desired))
            if remove:
                await ws.send(json.dumps({"action": "unsubscribe", "bars": remove,
                                          "updatedBars": remove}))
            if add:
                await ws.send(json.dumps({"action": "subscribe", "bars": add, "updatedBars": add}))

    async def run(self, stop: asyncio.Event, handler: Handler) -> None:
        backoff = self._backoff_initial
        while not stop.is_set():
            self.stats.state = "connecting"
            try:
                await self._session(stop, handler)
                backoff = self._backoff_initial  # clean close (stop requested)
            except StreamError as exc:
                self.stats.last_error = str(exc)
                if exc.code in FATAL_CODES:
                    self.stats.state = "fatal"
                    log.error("stream_fatal", code=exc.code, msg=exc.msg)
                    await handler(StreamEvent("fatal", detail=str(exc)))
                    backoff = self._backoff_max  # keep trying slowly; config may be fixed
                elif exc.code in SLOW_BACKOFF_CODES:
                    backoff = self._backoff_max
                log.warning("stream_error", code=exc.code, msg=exc.msg)
            except (ConnectionClosed, InvalidHandshake, InvalidURI, OSError, TimeoutError,
                    asyncio.IncompleteReadError, json.JSONDecodeError) as exc:
                self.stats.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("stream_disconnected", error=self.stats.last_error)
            finally:
                # _session() changes the state; read it through the method so the
                # type checker doesn't assume the value set before the call.
                state = self._state()
                self._ws = None
                self.stats.connected_since = None
                if state in ("connected", "connecting"):
                    await handler(StreamEvent("disconnected", detail=self.stats.last_error))
            if stop.is_set():
                break
            if self.stats.state != "fatal":
                self.stats.state = "backoff"
            delay = backoff * (0.8 + 0.4 * random.random())  # noqa: S311
            self.stats.reconnects += 1
            await self._wait(stop, delay)
            backoff = min(backoff * 2, self._backoff_max)
        self.stats.state = "stopped"

    # ------------------------------------------------------------------ internals

    def _state(self) -> State:
        return self.stats.state

    async def _wait(self, stop: asyncio.Event, delay: float) -> None:
        waiter = asyncio.ensure_future(stop.wait())
        sleeper = asyncio.ensure_future(self._sleep(delay))
        await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        for t in (waiter, sleeper):
            t.cancel()

    async def _recv(self, ws: WebSocketLike, wait_s: float | None = None
                    ) -> list[dict[str, Any]]:
        raw = await (asyncio.wait_for(ws.recv(), wait_s) if wait_s else ws.recv())
        self.stats.last_message_at = self._clock()
        msgs = json.loads(raw)
        if isinstance(msgs, dict):
            msgs = [msgs]
        for m in msgs:
            if m.get("T") == "error":
                raise StreamError(int(m.get("code", 0)), str(m.get("msg", "")))
        return list(msgs)

    async def _expect(self, ws: WebSocketLike, predicate: Callable[[dict[str, Any]], bool],
                      what: str) -> list[dict[str, Any]]:
        msgs = await self._recv(ws, AUTH_TIMEOUT_SECONDS)
        if not any(predicate(m) for m in msgs):
            raise StreamError(0, f"expected {what}, got {msgs!r}"[:300])
        return msgs

    async def _session(self, stop: asyncio.Event, handler: Handler) -> None:
        ws = await self._connect(self.url)
        try:
            await self._expect(ws, lambda m: m.get("T") == "success"
                               and m.get("msg") == "connected", "connected")
            await ws.send(json.dumps({"action": "auth", "key": self._creds.key_id,
                                      "secret": self._creds.secret_key}))
            await self._expect(ws, lambda m: m.get("T") == "success"
                               and m.get("msg") == "authenticated", "authenticated")
            async with self._lock:
                symbols = list(self._desired)
                self._ws = ws
                if symbols:
                    await ws.send(json.dumps({"action": "subscribe", "bars": symbols,
                                              "updatedBars": symbols}))
            if symbols:
                msgs = await self._expect(ws, lambda m: m.get("T") == "subscription",
                                          "subscription")
                self._apply_subscription(msgs)
            self.stats.state = "connected"
            self.stats.connected_since = self._clock()
            log.info("stream_connected", feed=self.feed, symbols=len(self.stats.subscribed))
            await handler(StreamEvent("connected"))
            await self._read_loop(ws, stop, handler)
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    def _apply_subscription(self, msgs: list[dict[str, Any]]) -> None:
        for m in msgs:
            if m.get("T") == "subscription":
                self.stats.subscribed = sorted(m.get("bars") or [])

    async def _read_loop(self, ws: WebSocketLike, stop: asyncio.Event, handler: Handler) -> None:
        stop_task = asyncio.ensure_future(stop.wait())
        try:
            while not stop.is_set():
                recv_task = asyncio.ensure_future(self._recv(ws))
                done, _ = await asyncio.wait({recv_task, stop_task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if recv_task not in done:
                    recv_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await recv_task
                    return
                for m in recv_task.result():
                    await self._dispatch(m, handler)
        finally:
            stop_task.cancel()

    async def _dispatch(self, m: dict[str, Any], handler: Handler) -> None:
        kind = m.get("T")
        if kind in ("b", "u"):
            bar = Bar.from_alpaca(str(m["S"]), self.feed, m, is_updated=kind == "u")
            await handler(StreamEvent("bar" if kind == "b" else "updated_bar", bar=bar, raw=m))
        elif kind == "s":
            await handler(StreamEvent("status", raw=m))
        elif kind == "subscription":
            self._apply_subscription([m])
