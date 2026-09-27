"""SimBroker: the Broker interface over the SimBroker fill model (docs/design/08 §10).

Driven by ``on_bar(bar)`` (replay and tests), it keeps orders and positions,
emits Alpaca-shaped trade updates (new, fill, partial_fill, canceled, replaced,
rejected), and enforces the same invariants as the paper account: long only
(a sell can't exceed the position), unique client_order_ids, bracket legs held
until the entry fills. Fault injection lets tests simulate network failures.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from intraday.marketdata.types import Bar
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
from intraday.trading.broker.sim import check_bracket, fill_entry, fill_market_exit

OPEN = frozenset({"new", "accepted", "held", "partially_filled", "pending_new"})


@dataclass
class _Order:
    id: str
    cid: str
    symbol: str
    side: str
    type: str
    order_class: str
    qty: int
    limit: Decimal | None = None
    stop: Decimal | None = None
    status: str = "new"
    filled_qty: int = 0
    avg: Decimal | None = None
    parent: str | None = None
    legs: list[str] = field(default_factory=list)
    created: datetime | None = None
    fill_bar_ts: datetime | None = None
    replaces: str | None = None

    def view(self, book: dict[str, _Order]) -> BrokerOrder:
        return BrokerOrder(self.id, self.cid, self.symbol, self.side, self.type,
                           self.order_class, self.qty, self.filled_qty, self.avg, self.status,
                           self.limit, self.stop, "day", self.created, self.created,
                           tuple(book[x].view(book) for x in self.legs), self.replaces)


class SimBroker(Broker):
    name = "sim"

    def __init__(self, cash: Decimal = Decimal("100000"), *, slippage_bps: int = 5) -> None:
        self.cash = cash
        self.slippage_bps = slippage_bps
        self.orders: dict[str, _Order] = {}
        self.positions: dict[str, tuple[int, Decimal]] = {}
        self.marks: dict[str, Decimal] = {}
        self.now: datetime | None = None
        self.queue: asyncio.Queue[TradeUpdate] = asyncio.Queue()
        self._ids = itertools.count(1)
        self._exec = itertools.count(1)
        # Fault injection: "before" raises without accepting; "after" accepts, then raises.
        self.fail_submit: str | None = None
        self.fail_reads = False

    # ------------------------------------------------------------------ helpers

    def _new_id(self) -> str:
        return f"sim-{next(self._ids):06d}"

    def _emit(self, event: str, o: _Order, price: Decimal | None = None,
              qty: int | None = None) -> None:
        pos = self.positions.get(o.symbol, (0, Decimal(0)))[0]
        self.queue.put_nowait(TradeUpdate(
            event, o.view(self.orders), self.now, price, qty, pos,
            f"exec-{next(self._exec)}" if event in ("fill", "partial_fill") else None))

    def _check_reads(self) -> None:
        if self.fail_reads:
            raise BrokerError(0, "network error: ConnectError", retryable=True)

    def _register(self, o: _Order, *, fault: bool = True) -> None:
        if any(x.cid == o.cid for x in self.orders.values()):
            raise BrokerError(422, "client_order_id must be unique")
        if fault and self.fail_submit == "before":
            self.fail_submit = None
            raise BrokerError(0, "network error: ConnectTimeout", retryable=True)
        o.created = self.now
        self.orders[o.id] = o

    def _after_accept(self) -> None:
        if self.fail_submit == "after":
            self.fail_submit = None
            raise BrokerError(0, "network error: ReadTimeout", retryable=True)

    def _held(self, symbol: str) -> int:
        return self.positions.get(symbol, (0, Decimal(0)))[0]

    def _committed_sells(self, symbol: str, excluding: set[str] | None = None) -> int:
        """Shares reserved by open sells. Like Alpaca, active exit legs reserve
        qty; the two legs of one bracket/OCO share it, so count each group once."""
        ex = excluding or set()
        groups: dict[str, int] = {}
        for o in self.orders.values():
            if (o.symbol != symbol or o.side != "sell" or o.status not in OPEN
                    or o.status == "held" or o.id in ex):
                continue
            root = self.orders.get(o.parent) if o.parent else None
            key = o.id if root is None else f"group:{root.id}"
            if root is None and o.order_class == "oco":
                key = f"group:{o.id}"
            groups[key] = max(groups.get(key, 0), o.qty - o.filled_qty)
        return sum(groups.values())

    # ------------------------------------------------------------------ reads

    async def get_account(self) -> BrokerAccount:
        self._check_reads()
        long_value = sum((q * self.marks.get(s, a) for s, (q, a) in self.positions.items()),
                         Decimal(0))
        eq = self.cash + long_value
        return BrokerAccount("SIM-PAPER", "ACTIVE", eq, self.cash, self.cash, eq, long_value,
                             False, False, "1")

    async def get_positions(self) -> list[BrokerPosition]:
        self._check_reads()
        return [BrokerPosition(s, q, a, self.marks.get(s)) for s, (q, a) in self.positions.items()
                if q > 0]

    async def get_open_orders(self) -> list[BrokerOrder]:
        self._check_reads()
        return [o.view(self.orders) for o in self.orders.values()
                if o.parent is None and (o.status in OPEN or any(
                    self.orders[x].status in OPEN for x in o.legs))]

    async def get_orders_since(self, after: datetime) -> list[BrokerOrder]:
        self._check_reads()
        return [o.view(self.orders) for o in self.orders.values()
                if o.parent is None and (o.created is None or o.created >= after)]

    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        self._check_reads()
        o = self.orders.get(broker_order_id)
        return o.view(self.orders) if o else None

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        self._check_reads()
        for o in self.orders.values():
            if o.cid == client_order_id:
                return o.view(self.orders)
        return None

    # ------------------------------------------------------------------ writes

    async def submit_bracket_entry(self, order: BracketEntry) -> BrokerOrder:
        parent = _Order(self._new_id(), order.client_order_id, order.symbol, "buy", "limit",
                        "bracket", order.qty, limit=order.limit_price)
        self._register(parent)
        tp = _Order(self._new_id(), f"{order.client_order_id}-tp", order.symbol, "sell",
                    "limit", "bracket", order.qty, limit=order.take_profit_price,
                    status="held", parent=parent.id)
        sl = _Order(self._new_id(), f"{order.client_order_id}-sl", order.symbol, "sell", "stop",
                    "bracket", order.qty, stop=order.stop_loss_price, status="held",
                    parent=parent.id)
        for leg in (tp, sl):
            self._register(leg, fault=False)
        parent.legs = [tp.id, sl.id]
        self._emit("new", parent)
        self._after_accept()
        return parent.view(self.orders)

    async def submit_market_exit(self, order: MarketExit) -> BrokerOrder:
        free = self._held(order.symbol) - self._committed_sells(order.symbol)
        if order.qty > free:
            raise BrokerError(403, f"insufficient qty available for order (requested "
                                   f"{order.qty}, available {free})")
        o = _Order(self._new_id(), order.client_order_id, order.symbol, "sell", "market",
                   "simple", order.qty)
        self._register(o)
        self._emit("new", o)
        self._after_accept()
        return o.view(self.orders)

    async def submit_protective_oco(self, order: ProtectiveOco) -> BrokerOrder:
        if order.qty > self._held(order.symbol) - self._committed_sells(order.symbol):
            raise BrokerError(403, "insufficient qty available for order")
        tp = _Order(self._new_id(), order.client_order_id, order.symbol, "sell", "limit", "oco",
                    order.qty, limit=order.take_profit_price)
        self._register(tp)
        sl = _Order(self._new_id(), f"{order.client_order_id}-sl", order.symbol, "sell", "stop",
                    "oco", order.qty, stop=order.stop_price, parent=tp.id)
        self._register(sl, fault=False)
        tp.legs = [sl.id]
        self._emit("new", tp)
        return tp.view(self.orders)

    async def cancel_order(self, broker_order_id: str) -> None:
        o = self.orders.get(broker_order_id)
        if o is None:
            raise BrokerError(404, "order not found")
        if o.status not in OPEN:
            raise BrokerError(422, f"order is already {o.status}")
        o.status = "canceled"
        self._emit("canceled", o)
        for leg in o.legs:  # cancelling a bracket parent cancels its legs
            lo = self.orders[leg]
            if lo.status in OPEN:
                lo.status = "canceled"
                self._emit("canceled", lo)

    async def replace_stop(self, broker_order_id: str, stop_price: Decimal) -> BrokerOrder:
        o = self.orders.get(broker_order_id)
        if o is None or o.type != "stop" or o.status not in OPEN:
            raise BrokerError(422, "order is not a replaceable open stop")
        new = replace(o, id=self._new_id(), cid=f"{o.cid}-r", stop=stop_price, legs=[],
                      replaces=o.id)
        o.status = "replaced"
        self.orders[new.id] = new
        if o.parent:
            parent = self.orders[o.parent]
            parent.legs = [new.id if x == o.id else x for x in parent.legs]
        self._emit("replaced", o)
        self._emit("new", new)
        return new.view(self.orders)

    async def configure_account(self) -> dict[str, Any]:
        return {"no_shorting": True, "fractional_trading": False, "max_margin_multiplier": "1"}

    async def run_trade_updates(self, stop: asyncio.Event, handler: TradeUpdateHandler) -> None:
        while not stop.is_set():
            get = asyncio.ensure_future(self.queue.get())
            stopper = asyncio.ensure_future(stop.wait())
            done, _ = await asyncio.wait({get, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if get in done:
                await handler(get.result())
            else:
                get.cancel()
            stopper.cancel()

    async def drain(self, handler: TradeUpdateHandler) -> int:
        """Deliver queued updates synchronously (tests / deterministic replay)."""
        n = 0
        while not self.queue.empty():
            await handler(self.queue.get_nowait())
            n += 1
        return n

    # ------------------------------------------------------------------ market simulation

    def _fill(self, o: _Order, price: Decimal, bar_ts: datetime) -> None:
        qty = o.qty - o.filled_qty
        held, avg = self.positions.get(o.symbol, (0, Decimal(0)))
        if o.side == "buy":
            new_qty = held + qty
            new_avg = (avg * held + price * qty) / new_qty
            self.positions[o.symbol] = (new_qty, new_avg.quantize(Decimal("0.0001")))
            self.cash -= price * qty
        else:
            if qty > held:
                o.status = "rejected"
                self._emit("rejected", o)
                return
            self.positions[o.symbol] = (held - qty, avg)
            self.cash += price * qty
            if held - qty == 0:
                self.positions.pop(o.symbol)
        o.filled_qty, o.avg, o.status, o.fill_bar_ts = o.qty, price, "filled", bar_ts
        self._emit("fill", o, price, qty)

    def on_bar(self, bar: Bar) -> None:
        self.now = bar.ts
        self.marks[bar.symbol] = bar.close
        for o in list(self.orders.values()):
            if o.symbol != bar.symbol or o.status not in OPEN or o.status == "held":
                continue
            if o.parent is None and o.side == "buy":
                f = fill_entry(bar, o.limit or Decimal(0), self.slippage_bps)
                if f is not None:
                    self._fill(o, f.price, bar.ts)
                    for leg in o.legs:          # legs activate once the entry has filled
                        self.orders[leg].status = "new"
                        self._emit("new", self.orders[leg])
                    self._check_legs(o, bar, entry_bar=True)
            elif o.type == "market":
                self._fill(o, fill_market_exit(bar, self.slippage_bps), bar.ts)
        for o in list(self.orders.values()):
            if (o.symbol == bar.symbol and o.parent is None and o.side == "buy"
                    and o.status == "filled" and o.fill_bar_ts != bar.ts):
                self._check_legs(o, bar)
            if o.symbol == bar.symbol and o.order_class == "oco" and o.parent is None:
                self._check_oco(o, bar)

    def _check_legs(self, parent: _Order, bar: Bar, *, entry_bar: bool = False) -> None:
        legs = [self.orders[x] for x in parent.legs]
        tp = next((x for x in legs if x.type == "limit"), None)
        sl = next((x for x in legs if x.type == "stop"), None)
        if tp is None or sl is None or tp.status not in OPEN or sl.status not in OPEN:
            return
        hit = check_bracket(bar, sl.stop or Decimal(0), tp.limit or Decimal(0),
                            self.slippage_bps, entry_bar=entry_bar)
        if hit is None:
            return
        win, lose = (sl, tp) if hit.reason == "STOP_LOSS" else (tp, sl)
        self._fill(win, hit.price, bar.ts)
        lose.status = "canceled"
        self._emit("canceled", lose)

    def _check_oco(self, tp: _Order, bar: Bar) -> None:
        sl = self.orders[tp.legs[0]] if tp.legs else None
        if sl is None or tp.status not in OPEN or sl.status not in OPEN:
            return
        hit = check_bracket(bar, sl.stop or Decimal(0), tp.limit or Decimal(0),
                            self.slippage_bps)
        if hit is None:
            return
        win, lose = (sl, tp) if hit.reason == "STOP_LOSS" else (tp, sl)
        self._fill(win, hit.price, bar.ts)
        lose.status = "canceled"
        self._emit("canceled", lose)
