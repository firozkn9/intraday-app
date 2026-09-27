"""Bar stream against a fake Alpaca WebSocket server: auth, subscribe, bars, reconnect."""

import asyncio
from collections.abc import Callable

import pytest

from intraday.marketdata.alpaca.http import Credentials
from intraday.marketdata.alpaca.stream import AlpacaBarStream, StreamEvent
from tests.fakes import FakeAlpacaStream, bar_msg

CREDS = Credentials("key", "secret")


async def no_sleep(_: float) -> None:
    await asyncio.sleep(0)


class Collector:
    def __init__(self) -> None:
        self.events: list[StreamEvent] = []
        self.changed = asyncio.Event()

    async def __call__(self, ev: StreamEvent) -> None:
        self.events.append(ev)
        self.changed.set()

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    async def wait_for(self, pred: Callable[[], bool], limit_s: float = 5) -> None:
        async def loop() -> None:
            while not pred():
                self.changed.clear()
                await self.changed.wait()
        await asyncio.wait_for(loop(), limit_s)


async def start(stream: AlpacaBarStream, events: Collector) -> tuple[asyncio.Event, asyncio.Task]:
    stop = asyncio.Event()
    return stop, asyncio.create_task(stream.run(stop, events))


async def finish(stop: asyncio.Event, task: asyncio.Task) -> None:
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_auth_subscribe_and_receive_bars() -> None:
    async with FakeAlpacaStream() as server:
        stream = AlpacaBarStream(CREDS, "iex", url=server.url, sleep=no_sleep)
        await stream.set_symbols(["SPY", "AAPL"])
        events = Collector()
        stop, task = await start(stream, events)
        await events.wait_for(lambda: "connected" in events.kinds())
        assert server.subscribed == {"SPY", "AAPL"}
        assert server.actions[0] == {"action": "auth", "key": "key", "secret": "secret"}
        assert server.actions[1] == {"action": "subscribe", "bars": ["AAPL", "SPY"],
                                     "updatedBars": ["AAPL", "SPY"]}
        assert stream.stats.state == "connected" and stream.stats.subscribed == ["AAPL", "SPY"]

        await server.push([bar_msg("SPY", "2026-09-21T13:30:00Z", 571.5),
                           bar_msg("AAPL", "2026-09-21T13:30:00Z", 228.9, kind="u")])
        await events.wait_for(lambda: len([e for e in events.events if e.bar]) == 2)
        bars = [e for e in events.events if e.bar]
        assert [b.kind for b in bars] == ["bar", "updated_bar"]
        assert bars[0].bar.symbol == "SPY" and bars[0].bar.feed == "iex"
        assert bars[1].bar.is_updated is True
        await finish(stop, task)
        assert stream.stats.state == "stopped"


async def test_live_resubscribe_on_symbol_change() -> None:
    async with FakeAlpacaStream() as server:
        stream = AlpacaBarStream(CREDS, "iex", url=server.url, sleep=no_sleep)
        await stream.set_symbols(["SPY", "AAPL"])
        events = Collector()
        stop, task = await start(stream, events)
        await events.wait_for(lambda: "connected" in events.kinds())
        server.subscribed_event.clear()
        await stream.set_symbols(["SPY", "MSFT"])
        await asyncio.wait_for(server.subscribed_event.wait(), 5)
        await asyncio.sleep(0.05)
        assert server.subscribed == {"SPY", "MSFT"}
        unsubscribe = {"action": "unsubscribe", "bars": ["AAPL"], "updatedBars": ["AAPL"]}
        assert unsubscribe in server.actions
        await finish(stop, task)


async def test_reconnects_after_drop_and_reports_disconnect() -> None:
    async with FakeAlpacaStream() as server:
        stream = AlpacaBarStream(CREDS, "iex", url=server.url, sleep=no_sleep,
                                 backoff_initial=0.01)
        await stream.set_symbols(["SPY"])
        events = Collector()
        stop, task = await start(stream, events)
        await events.wait_for(lambda: events.kinds().count("connected") == 1)
        await server.drop_all()
        await events.wait_for(lambda: events.kinds().count("connected") == 2)
        assert "disconnected" in events.kinds()
        assert server.connections == 2 and stream.stats.reconnects >= 1
        assert server.subscribed == {"SPY"}  # resubscribed after reconnect
        await finish(stop, task)


async def test_bad_credentials_are_fatal_and_back_off() -> None:
    sleeps: list[float] = []

    async def record(s: float) -> None:
        sleeps.append(s)
        await asyncio.sleep(0)

    async with FakeAlpacaStream(key="other") as server:
        stream = AlpacaBarStream(CREDS, "iex", url=server.url, sleep=record, backoff_max=60)
        await stream.set_symbols(["SPY"])
        events = Collector()
        stop, task = await start(stream, events)
        await events.wait_for(lambda: "fatal" in events.kinds())
        assert stream.stats.state == "fatal" and "402" in (stream.stats.last_error or "")
        await events.wait_for(lambda: len(sleeps) >= 1)
        assert sleeps[0] >= 60 * 0.8  # fatal errors retry at the slowest pace
        await finish(stop, task)


async def test_symbol_limit_error_is_fatal() -> None:
    async with FakeAlpacaStream(symbol_limit=1) as server:
        stream = AlpacaBarStream(CREDS, "iex", url=server.url, sleep=no_sleep)
        await stream.set_symbols(["SPY", "QQQ"])
        events = Collector()
        stop, task = await start(stream, events)
        await events.wait_for(lambda: "fatal" in events.kinds())
        assert "405" in (stream.stats.last_error or "")
        await finish(stop, task)


async def test_unreachable_server_backs_off_exponentially() -> None:
    sleeps: list[float] = []

    async def record(s: float) -> None:
        sleeps.append(s)
        await asyncio.sleep(0)

    async def refuse(url: str) -> None:  # immediate refusal; OS timing varies by platform
        raise ConnectionRefusedError("refused")

    stream = AlpacaBarStream(CREDS, "iex", url="ws://unreachable.invalid", sleep=record,
                             connect=refuse, backoff_initial=1, backoff_max=8)  # type: ignore[arg-type]
    events = Collector()
    stop, task = await start(stream, events)
    await events.wait_for(lambda: len(sleeps) >= 5, limit_s=10)
    await finish(stop, task)
    # 1, 2, 4, 8, 8 (±20% jitter)
    for got, base in zip(sleeps[:5], [1, 2, 4, 8, 8], strict=True):
        assert base * 0.8 <= got <= base * 1.2
    assert "connected" not in events.kinds()


@pytest.mark.parametrize("feed", ["iex", "sip"])
def test_default_url_per_feed(feed: str) -> None:
    assert AlpacaBarStream(CREDS, feed).url == f"wss://stream.data.alpaca.markets/v2/{feed}"
