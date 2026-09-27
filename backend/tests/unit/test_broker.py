"""Phase 7: AlpacaPaperBroker (respx + fake trade stream), factory guard, SimBroker."""

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from websockets.asyncio.server import ServerConnection, serve

from intraday.core.env import EnvSettings
from intraday.core.safety import PaperModeViolation
from intraday.marketdata.alpaca.http import AlpacaHttp, Credentials, TokenBucket
from intraday.trading.broker.alpaca_paper import AlpacaPaperBroker
from intraday.trading.broker.base import (
    BracketEntry,
    BrokerError,
    BrokerOrder,
    MarketExit,
    TradeUpdate,
)
from intraday.trading.broker.factory import BrokerConfigError, build_broker
from intraday.trading.broker.sim_broker import SimBroker
from tests.fakes import fixture
from tests.session_fixtures import SESSION, bars

PAPER = "https://paper-api.alpaca.markets"
CREDS = Credentials("key", "secret")
D = Decimal


async def no_sleep(_: float) -> None:
    return None


def broker(url: str = PAPER, **kw: Any) -> AlpacaPaperBroker:
    http = AlpacaHttp(url, CREDS, limiter=TokenBucket(100_000), sleep=no_sleep)
    return AlpacaPaperBroker(http, CREDS, trading_mode="PAPER", **kw)


ENTRY = BracketEntry("ia-20260921-EN-7f3a9c21-0", "AAPL", 90, D("101.30"), D("103.28"),
                     D("100.24"))


# ------------------------------------------------------------------ REST

@respx.mock
async def test_bracket_entry_request_body_and_parse() -> None:
    route = respx.post(f"{PAPER}/v2/orders").mock(
        return_value=httpx.Response(200, json=fixture("order_bracket.json")))
    order = await broker().submit_bracket_entry(ENTRY)
    body = json.loads(route.calls[0].request.content)
    assert body == {"symbol": "AAPL", "qty": "90", "side": "buy", "type": "limit",
                    "time_in_force": "day", "limit_price": "101.30", "order_class": "bracket",
                    "client_order_id": "ia-20260921-EN-7f3a9c21-0",
                    "take_profit": {"limit_price": "103.28"},
                    "stop_loss": {"stop_price": "100.24"}}
    assert order.status == "accepted" and order.qty == 90 and order.order_class == "bracket"
    assert [(leg.type, leg.status) for leg in order.legs] == [("limit", "held"), ("stop", "held")]
    assert order.legs[1].stop_price == D("100.24")


@respx.mock
async def test_order_submission_is_never_retried_blindly() -> None:
    route = respx.post(f"{PAPER}/v2/orders").mock(return_value=httpx.Response(503))
    with pytest.raises(BrokerError) as exc:
        await broker().submit_bracket_entry(ENTRY)
    assert route.call_count == 1 and exc.value.retryable


@respx.mock
async def test_rejections_are_not_retryable() -> None:
    respx.post(f"{PAPER}/v2/orders").mock(return_value=httpx.Response(
        403, json={"message": "insufficient buying power"}))
    with pytest.raises(BrokerError) as exc:
        await broker().submit_market_exit(MarketExit("x", "AAPL", 10))
    assert exc.value.status == 403 and not exc.value.retryable


@respx.mock
async def test_lookup_by_client_id_and_404() -> None:
    respx.get(f"{PAPER}/v2/orders:by_client_order_id").mock(side_effect=[
        httpx.Response(200, json=fixture("order_bracket.json")),
        httpx.Response(404, json={"message": "order not found"})])
    b = broker()
    found = await b.get_order_by_client_id("ia-20260921-EN-7f3a9c21-0")
    assert found is not None and found.id.startswith("61e69015")
    assert await b.get_order_by_client_id("missing") is None


@respx.mock
async def test_account_positions_cancel_replace() -> None:
    respx.get(f"{PAPER}/v2/account").mock(return_value=httpx.Response(
        200, json=fixture("account.json")))
    respx.get(f"{PAPER}/v2/positions").mock(return_value=httpx.Response(
        200, json=fixture("positions.json")))
    cancel = respx.delete(f"{PAPER}/v2/orders/abc").mock(return_value=httpx.Response(204))
    repl = respx.patch(f"{PAPER}/v2/orders/sl1").mock(return_value=httpx.Response(
        200, json={**fixture("order_bracket.json")["legs"][1], "id": "sl2",
                   "stop_price": "101.60"}))
    b = broker()
    acct = await b.get_account()
    assert acct.equity == D("100000.00") and acct.multiplier == "1" and not acct.shorting_enabled
    (pos,) = await b.get_positions()
    assert (pos.symbol, pos.qty, pos.avg_entry_price) == ("AAPL", 90, D("101.30"))
    await b.cancel_order("abc")
    assert cancel.called
    new = await b.replace_stop("sl1", D("101.6"))
    assert json.loads(repl.calls[0].request.content) == {"stop_price": "101.60"}
    assert new.id == "sl2"


@respx.mock
async def test_configure_account_is_verified() -> None:
    route = respx.patch(f"{PAPER}/v2/account/configurations")
    route.mock(return_value=httpx.Response(200, json={
        "no_shorting": True, "fractional_trading": False, "max_margin_multiplier": "1"}))
    assert (await broker().configure_account())["no_shorting"] is True
    assert json.loads(route.calls[0].request.content) == {
        "no_shorting": True, "fractional_trading": False, "max_margin_multiplier": "1"}
    route.mock(return_value=httpx.Response(200, json={
        "no_shorting": False, "fractional_trading": False, "max_margin_multiplier": "4"}))
    with pytest.raises(BrokerError, match="not applied"):
        await broker().configure_account()


def test_broker_refuses_non_paper_endpoint() -> None:
    with pytest.raises(PaperModeViolation):
        broker("https://api.alpaca.markets")


def test_trade_update_parsing() -> None:
    u = TradeUpdate.from_alpaca(fixture("trade_update_fill.json")["data"])
    assert (u.event, u.qty, u.position_qty, u.price) == ("fill", 90, 90, D("101.30"))
    assert u.execution_id and u.order.filled_qty == 90 and u.order.terminal


# ------------------------------------------------------------------ trade stream

class FakeTradeStream:
    def __init__(self, *, authorize: bool = True) -> None:
        self.authorize = authorize
        self.connections = 0
        self.clients: set[ServerConnection] = set()

    async def handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        self.clients.add(ws)
        try:
            json.loads(await ws.recv())  # auth
            status = "authorized" if self.authorize else "unauthorized"
            await ws.send(json.dumps({"stream": "authorization", "data": {
                "status": status, "action": "authenticate"}}).encode())   # binary frames
            if not self.authorize:
                await ws.close()
                return
            json.loads(await ws.recv())  # listen
            await ws.send(json.dumps({"stream": "listening",
                                      "data": {"streams": ["trade_updates"]}}).encode())
            await ws.wait_closed()
        finally:
            self.clients.discard(ws)

    async def push(self, msg: dict[str, Any]) -> None:
        for ws in list(self.clients):
            await ws.send(json.dumps(msg).encode())


async def test_trade_stream_auth_listen_and_updates() -> None:
    fake = FakeTradeStream()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        states: list[str] = []

        async def on_state(s: str) -> None:
            states.append(s)

        b = broker(stream_url=f"ws://127.0.0.1:{port}", on_stream_state=on_state)
        got: list[TradeUpdate] = []

        async def handle(u: TradeUpdate) -> None:
            got.append(u)

        stop = asyncio.Event()
        task = asyncio.create_task(b.run_trade_updates(stop, handle))
        for _ in range(100):
            if "connected" in states:
                break
            await asyncio.sleep(0.02)
        await fake.push(fixture("trade_update_fill.json"))
        for _ in range(100):
            if got:
                break
            await asyncio.sleep(0.02)
        assert got[0].event == "fill" and got[0].order.symbol == "AAPL"
        stop.set()
        await asyncio.wait_for(task, 5)
        assert states[0] == "connected" and states[-1] == "stopped"


async def test_trade_stream_bad_auth_backs_off() -> None:
    fake = FakeTradeStream(authorize=False)
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        b = broker(stream_url=f"ws://127.0.0.1:{port}")
        stop = asyncio.Event()
        task = asyncio.create_task(b.run_trade_updates(stop, lambda u: asyncio.sleep(0)))
        for _ in range(100):
            if fake.connections:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        assert fake.connections == 1          # waits (60 s) before retrying bad credentials
        stop.set()
        await asyncio.wait_for(task, 5)


# ------------------------------------------------------------------ factory

def env(**kw: Any) -> EnvSettings:
    base = {"TRADING_MODE": "PAPER", "ALPACA_BASE_URL": PAPER, "ALPACA_API_KEY_ID": "PKREAL1",
            "ALPACA_API_SECRET_KEY": "realsecret123"}
    return EnvSettings.model_validate({**base, **kw})


def test_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENGINE_BROKER", raising=False)
    assert isinstance(build_broker(env()), AlpacaPaperBroker)
    with pytest.raises(PaperModeViolation):
        build_broker(env(ALPACA_BASE_URL="https://api.alpaca.markets"))
    with pytest.raises(PaperModeViolation):
        build_broker(env(TRADING_MODE="LIVE"))
    with pytest.raises(BrokerConfigError, match="not set"):
        build_broker(env(ALPACA_API_KEY_ID="PK_PLACEHOLDER"))
    monkeypatch.setenv("ENGINE_BROKER", "sim")
    with pytest.raises(BrokerConfigError, match="only allowed with ENGINE_DATA=replay"):
        build_broker(env())
    monkeypatch.setenv("ENGINE_DATA", "replay")
    assert isinstance(build_broker(env()), SimBroker)
    monkeypatch.setenv("ENGINE_BROKER", "live")
    with pytest.raises(BrokerConfigError):
        build_broker(env())


# ------------------------------------------------------------------ SimBroker

def sim_bars(closes: list[float]) -> list:  # type: ignore[type-arg]
    return bars("AAPL", closes, 1000.0)


async def collect(sb: SimBroker) -> list[TradeUpdate]:
    out: list[TradeUpdate] = []

    async def h(u: TradeUpdate) -> None:
        out.append(u)

    await sb.drain(h)
    return out


async def test_sim_bracket_lifecycle_take_profit() -> None:
    sb = SimBroker()
    await sb.submit_bracket_entry(BracketEntry("c1", "AAPL", 10, D("100.10"), D("101"),
                                               D("99")))
    b = sim_bars([100.0, 100.5, 101.5])
    for x in b:
        sb.on_bar(x)
    events = [(u.event, u.order.side, u.order.type) for u in await collect(sb)]
    assert events == [("new", "buy", "limit"), ("fill", "buy", "limit"),
                      ("new", "sell", "limit"), ("new", "sell", "stop"),   # legs activate
                      ("fill", "sell", "limit"), ("canceled", "sell", "stop")]
    assert not await sb.get_positions() and sb.cash > D("100000")


async def test_sim_stop_cancel_replace_and_invariants() -> None:
    sb = SimBroker()
    parent = await sb.submit_bracket_entry(BracketEntry("c1", "AAPL", 10, D("100.10"),
                                                        D("105"), D("99")))
    with pytest.raises(BrokerError, match="unique"):
        await sb.submit_bracket_entry(BracketEntry("c1", "AAPL", 1, D("1"), D("2"), D("0.5")))
    sb.on_bar(sim_bars([100.0])[0])
    stop_leg = next(x for x in (await sb.get_order(parent.id)).legs if x.type == "stop")  # type: ignore[union-attr]
    new = await sb.replace_stop(stop_leg.id, D("99.5"))
    assert new.stop_price == D("99.5") and (await sb.get_order(stop_leg.id)).status == "replaced"  # type: ignore[union-attr]
    with pytest.raises(BrokerError, match="insufficient qty"):
        await sb.submit_market_exit(MarketExit("x1", "AAPL", 11))   # can't sell more than held
    with pytest.raises(BrokerError, match="insufficient qty"):
        await sb.submit_market_exit(MarketExit("x2", "AAPL", 10))   # open legs reserve the shares
    await sb.cancel_order(new.id)
    tp = next(x for x in (await sb.get_order(parent.id)).legs if x.type == "limit")  # type: ignore[union-attr]
    await sb.cancel_order(tp.id)
    await sb.submit_market_exit(MarketExit("x3", "AAPL", 10))
    sb.on_bar(bars("AAPL", [100.0, 99.0], 1000.0)[1])
    assert not await sb.get_positions()
    with pytest.raises(BrokerError, match="already"):
        await sb.cancel_order(new.id)


async def test_sim_fault_injection() -> None:
    sb = SimBroker()
    sb.fail_submit = "after"
    with pytest.raises(BrokerError) as exc:
        await sb.submit_bracket_entry(BracketEntry("c9", "AAPL", 1, D("1"), D("2"), D("0.5")))
    assert exc.value.retryable
    assert await sb.get_order_by_client_id("c9") is not None   # accepted despite the error
    sb.fail_submit = "before"
    with pytest.raises(BrokerError):
        await sb.submit_bracket_entry(BracketEntry("c10", "AAPL", 1, D("1"), D("2"),
                                                   D("0.5")))
    assert await sb.get_order_by_client_id("c10") is None


def test_order_view_types() -> None:
    o = BrokerOrder.from_alpaca(fixture("order_bracket.json"))
    assert not o.terminal and o.submitted_at is not None
    assert SESSION.open_at.tzinfo is not None
