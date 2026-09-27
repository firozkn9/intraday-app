"""Phases 8-12 end to end: submission from the decision loop, VETO/EXPLAIN, runtime with a
broker, emergency stop, reports/system APIs, alert delivery."""

from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select

from intraday.ai.providers import CallRecord, ExplainResult, ProviderError, VetoResult
from intraday.ai.service import AIService
from intraday.alerts.notifier import dispatch
from intraday.core.env import EnvSettings
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import AiDecision, Alert, EngineCommand, LlmCall, Order, Position
from intraday.db.repositories import engine as engine_repo
from intraday.db.session import transaction
from intraday.engine.decisions import DecisionService
from intraday.engine.runtime import EngineRuntime
from intraday.trading.broker.sim_broker import SimBroker
from intraday.trading.order_manager import EntryRequest, OrderManager
from tests.integration.test_decisions import AT, CAL, seed_history
from tests.integration.test_risk_integration import adapter, add_symbol, quotes
from tests.integration.test_trading import bar, entry, no_sleep

API = "/api/v1"
CFG = TradingConfig()


def om_for(sm: Any, sim: SimBroker) -> OrderManager:
    return OrderManager(sm, sim, clock=lambda: AT, sleep=no_sleep, retry_delays=(0, 0, 0))


def submitter(om: OrderManager):  # type: ignore[no-untyped-def]
    async def submit(did: Any, rd: Any, rec: Any) -> tuple[str, list[str]]:
        p = rd.proposal
        res = await om.submit_entry(EntryRequest(did, AT.date(), p.symbol, rd.qty, p.entry,
                                                 p.stop, p.target, strategy=rec.strategy))
        return ("ORDER_SUBMITTED", []) if res.status == "SUBMITTED" else (
            "ORDER_FAILED", [res.status])
    return submit


async def decision(sm: Any) -> AiDecision:
    async with sm() as s:
        return (await s.execute(select(AiDecision))).scalars().one()


async def test_approved_proposal_is_submitted_as_bracket(sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    await add_symbol(sessionmaker)
    sim = SimBroker()
    svc = DecisionService(sessionmaker, feed="iex", clock=lambda: AT)
    await svc.cycle(CAL, ["AAA"], CFG, 3, [], risk=adapter(sessionmaker, quotes()),
                    submit=submitter(om_for(sessionmaker, sim)))
    dec = await decision(sessionmaker)
    assert dec.decision_result == "ORDER_SUBMITTED" and dec.block_reasons == []
    async with sessionmaker() as s:
        rows = list((await s.execute(select(Order))).scalars())
        pos = (await s.execute(select(Position))).scalars().one()
    entry_row = next(o for o in rows if o.intent == "ENTRY")
    assert entry_row.order_class == "bracket" and entry_row.decision_id == dec.id
    assert pos.status == "PENDING_ENTRY" and pos.entry_decision_id == dec.id
    parent = next(o for o in sim.orders.values() if o.parent is None)
    # The limit is the priced entry the risk engine sized: <= reference + 5 bps.
    assert parent.limit is not None and parent.limit <= dec.entry_ref_price * Decimal("1.0005")
    assert parent.qty == entry_row.qty


class FakeProvider:
    name, model = "fake", "fake-model"

    def __init__(self, verdict: str | None = "APPROVE", text: str | None = None) -> None:
        self.verdict, self.text = verdict, text

    async def review_trade(self, req: Any) -> tuple[VetoResult, CallRecord]:
        rec = CallRecord("VETO", self.name, self.model, req.model_dump())
        if self.verdict is None:
            rec.error = "timeout"
            raise ProviderError("timeout", rec)
        rec.valid = True
        return VetoResult(verdict=self.verdict, reasons=["test"]), rec  # type: ignore[arg-type]

    async def rephrase(self, req: Any) -> tuple[ExplainResult, CallRecord]:
        rec = CallRecord("EXPLAIN", self.name, self.model, req.model_dump(), valid=True)
        return ExplainResult(text=self.text or req.template), rec

    async def classify_news(self, req: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def veto_cfg(mode: str = "VETO") -> TradingConfig:
    return TradingConfig.model_validate({"ai": {"mode": mode, "model": "fake-model"}})


async def test_veto_reject_and_error_remove_the_trade(sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    await add_symbol(sessionmaker)
    ai = AIService(sessionmaker, FakeProvider("REJECT"), max_consecutive_failures=1)
    svc = DecisionService(sessionmaker, feed="iex", clock=lambda: AT)
    await svc.cycle(CAL, ["AAA"], veto_cfg(), 3, [], risk=adapter(sessionmaker, quotes()), ai=ai)
    dec = await decision(sessionmaker)
    assert dec.decision_result == "VETOED" and dec.llm_verdict == "REJECT"
    assert dec.llm_call_id is not None and not ai.unavailable

    ai2 = AIService(sessionmaker, FakeProvider(None), max_consecutive_failures=1)
    await ai2.veto(SimpleRec.from_decision(), [])
    assert ai2.unavailable
    async with sessionmaker() as s:
        calls = list((await s.execute(select(LlmCall))).scalars())
    assert [c.valid for c in calls].count(False) == 1


class SimpleRec:
    @staticmethod
    def from_decision() -> Any:
        from types import SimpleNamespace
        p = SimpleNamespace(entry_ref_price=100.0, stop_loss=99.0, take_profit=102.0,
                            signals=[], risk_reward_ratio=2.0, market_context={})
        return SimpleNamespace(symbol="AAA", strategy="pullback", score=0.8, factor_scores={},
                               proposal=p, features={})


async def test_explain_keeps_guarded_text_only(sessionmaker: Any) -> None:
    await seed_history(sessionmaker)
    await add_symbol(sessionmaker)
    svc = DecisionService(sessionmaker, feed="iex", clock=lambda: AT)
    ai = AIService(sessionmaker, FakeProvider(text="Invented 42% upside."))
    await svc.cycle(CAL, ["AAA"], veto_cfg("EXPLAIN"), 3, ["PAUSED_BY_USER"],
                    risk=adapter(sessionmaker, quotes()), ai=ai)
    (did, rec, template) = svc.explain_queue[0]
    assert await ai.explain(did, rec, template) is None           # number guard rejected it
    ai.provider = FakeProvider(text=None)                           # faithful: template itself
    assert await ai.explain(did, rec, template) == template
    assert (await decision(sessionmaker)).reasoning_llm == template
    assert (await decision(sessionmaker)).decision_result == "BLOCKED"   # never changes it


# ------------------------------------------------------------------ runtime with a broker

def runtime(env: EnvSettings, sm: Any, sim: SimBroker) -> EngineRuntime:
    return EngineRuntime(env, sm, None, TradingConfig(), broker_factory=lambda: sim,
                         order_manager_options={"sleep": no_sleep, "retry_delays": (0, 0, 0)},
                         sleep=no_sleep)


async def test_runtime_boot_reconciles_and_emergency_stop_flattens(
        env: EnvSettings, sessionmaker: Any) -> None:
    sim = SimBroker()
    rt = runtime(env, sessionmaker, sim)
    await rt.boot()
    assert "NO_BROKER" not in rt.gate.blocks and "RECONCILING" not in rt.gate.blocks
    assert rt.om is not None and rt.liquidator is not None
    await add_symbol(sessionmaker)
    req = await entry(sessionmaker, symbol="AAA")
    rt.om.entry_blocks = None
    assert (await rt.om.submit_entry(req)).status == "SUBMITTED"
    sim.on_bar(bar(11, 100.0, 100.3, 99.9, 100.2))
    await sim.drain(rt.om.on_trade_update)
    assert sim.positions["AAA"][0] == 10

    async with transaction(sessionmaker) as s:
        cmd = await engine_repo.enqueue_command(s, command="EMERGENCY_STOP", user_id=None)

    async def fill_exits(_: float) -> None:            # the market fills while we wait
        sim.on_bar(bar(12, 100.5, 100.6, 100.4, 100.5))
        await sim.drain(rt.om.on_trade_update)  # type: ignore[union-attr]

    rt.liquidator.sleep = fill_exits
    await rt.process_commands_once()
    await rt.wait_background()
    assert "EMERGENCY_STOP" in rt.gate.latched and not sim.positions
    async with sessionmaker() as s:
        done = await s.get(EngineCommand, cmd.id)
        pos = (await s.execute(select(Position))).scalars().one()
    assert done.status == "DONE" and done.result["flat"] is True
    assert pos.status == "CLOSED" and pos.exit_reason == "EMERGENCY"


async def test_runtime_unexpected_position_latches(env: EnvSettings, sessionmaker: Any) -> None:
    sim = SimBroker()
    sim.positions["ZZZ"] = (3, Decimal("10"))
    rt = runtime(env, sessionmaker, sim)
    await rt.boot()
    assert "RECONCILE_MISMATCH" in rt.gate.latched
    async with sessionmaker() as s:
        alerts = [a.type for a in (await s.execute(select(Alert))).scalars()]
    assert "RECONCILE_MISMATCH" in alerts


# ------------------------------------------------------------------ APIs

async def test_report_and_system_endpoints(auth_client: httpx.AsyncClient,
                                           sessionmaker: Any) -> None:
    for path in ("/dashboard/summary", "/dashboard/equity-intraday", "/positions",
                 "/positions?scope=today", "/trades", "/calendar?month=2026-09",
                 "/calendar/2026-09-21", "/performance/summary", "/performance/pnl?period=week",
                 "/performance/equity-curve", "/performance/breakdown?by=hour", "/account",
                 "/system/health", "/logs", "/audit", "/alerts?state=all", "/news?symbol=AAA",
                 "/metrics"):
        r = await auth_client.get(API + path)
        assert r.status_code == 200, (path, r.text)
    csv = await auth_client.get(API + "/trades/export.csv")
    assert csv.status_code == 200 and csv.text.startswith("id,session_date,symbol")


async def test_controls_require_typed_confirmation(auth_client: httpx.AsyncClient,
                                                   sessionmaker: Any) -> None:
    csrf = auth_client.cookies.get("csrf") or ""
    h = {"X-CSRF-Token": csrf}
    r = await auth_client.post(API + "/controls/emergency-stop", json={"confirm": "x"},
                               headers=h)
    assert r.status_code == 422
    r = await auth_client.post(API + "/controls/emergency-stop",
                               json={"confirm": "EMERGENCY STOP", "close_positions": False},
                               headers=h)
    assert r.status_code == 202
    r = await auth_client.post(API + "/controls/close-all", json={"confirm": "CLOSE ALL"},
                               headers=h)
    assert r.status_code == 202
    assert (await auth_client.post(API + "/controls/reconcile-now", headers=h)).status_code == 202
    async with sessionmaker() as s:
        cmds = {c.command: c.params for c in (await s.execute(select(EngineCommand))).scalars()}
    assert cmds["EMERGENCY_STOP"] == {"close_positions": False} and "CLOSE_ALL" in cmds

    async with transaction(sessionmaker) as s:
        s.add(Alert(severity="HIGH", type="NOT_FLAT", message="x"))
    alert = (await auth_client.get(API + "/alerts")).json()[0]
    acked = await auth_client.post(API + f"/alerts/{alert['id']}/ack", headers=h)
    assert acked.status_code == 200 and acked.json()["acknowledged_at"]


async def test_alert_dispatch_once_per_channel(sessionmaker: Any) -> None:
    sent: list[str] = []

    class Fake:
        channel = "email"

        async def send(self, a: Alert) -> None:
            sent.append(a.type)

    async with transaction(sessionmaker) as s:
        s.add_all([Alert(severity="HIGH", type="A", message="a"),
                   Alert(severity="INFO", type="B", message="b")])
    assert await dispatch(sessionmaker, [Fake()], min_severity="WARN") == 1
    assert await dispatch(sessionmaker, [Fake()], min_severity="WARN") == 0
    assert sent == ["A"]

