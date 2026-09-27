"""Decision loop (docs/design/08 §3): one signal-pipeline cycle per completed minute.

- ``load_contexts`` builds per-symbol session context (previous close, daily ATR,
  ADV, same-feed intraday volume profile, warm-up bars) from the database. It is
  shared with the offline ``intraday signals`` replay.
- ``DecisionService.cycle`` runs once per completed 1-minute bar during the
  session, persists every evaluation (trading_signals) and every proposal
  (ai_decisions), and runs every proposal through the risk engine
  (risk_evaluations). Approved proposals go to the order manager, in score
  order, through ``submit``: ORDER_SUBMITTED or ORDER_FAILED. Others end as
  BLOCKED (only the gate stopped them) or RISK_REJECTED (with the failed
  check IDs).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.ai.news import news_factor
from intraday.ai.pipeline import CycleResult, SignalRecord, run_cycle
from intraday.ai.service import AIService
from intraday.core.ids import uuid7
from intraday.core.logging import get_logger
from intraday.core.runtime_config import TradingConfig
from intraday.core.timeutil import ET, utcnow
from intraday.db.models import AiDecision, RiskEvaluation
from intraday.db.repositories import signals as sig_repo
from intraday.db.session import transaction
from intraday.engine.risk_adapter import RiskAdapter
from intraday.features.feature_engineering import DailyStat, SymbolContext, build_context
from intraday.features.regime import REFERENCE
from intraday.marketdata.calendar import MarketCalendar, SessionDay
from intraday.marketdata.news import recent_news
from intraday.marketdata.types import BAR_INTERVAL, Bar
from intraday.risk.models import GateSnapshot, RiskDecision, RiskProposal

log = get_logger(__name__)

HISTORY_SESSIONS = 20
DAILY_LOOKBACK_DAYS = 60
BAR_GRACE = timedelta(seconds=5)   # wait this long after a minute ends for its bars

# (decision id, risk decision, signal record) -> (decision_result, block_reasons)
Submitter = Callable[[Any, RiskDecision, SignalRecord], Awaitable[tuple[str, list[str]]]]


def _bar(r: Any) -> Bar:
    return Bar(r.symbol, r.feed, r.ts, r.open, r.high, r.low, r.close, r.volume, r.vwap,
               r.trade_count)


async def load_contexts(sm: async_sessionmaker[AsyncSession], calendar: MarketCalendar,
                        feed: str, session: SessionDay, symbols: Sequence[str],
                        adv_lookback: int = 20) -> dict[str, SymbolContext]:
    prior = [s for s in calendar.sessions_between(session.session_date - timedelta(days=45),
                                                  session.session_date - timedelta(days=1))]
    prior = prior[-HISTORY_SESSIONS:]
    async with sm() as s:
        daily = await sig_repo.daily_stats(
            s, feed, symbols, session.session_date - timedelta(days=DAILY_LOOKBACK_DAYS),
            session.session_date)
        minute = (await sig_repo.minute_bars(s, feed, symbols, prior[0].open_at,
                                             prior[-1].close_at - BAR_INTERVAL)
                  if prior else [])
    by_daily: dict[str, list[DailyStat]] = defaultdict(list)
    for d in daily:
        by_daily[d.symbol].append(DailyStat(d.session_date, float(d.high), float(d.low),
                                            float(d.close), float(d.volume)))
    by_session: dict[str, dict[date, list[Bar]]] = defaultdict(lambda: defaultdict(list))
    for r in minute:
        by_session[r.symbol][r.ts.astimezone(ET).date()].append(_bar(r))
    out = {}
    for sym in symbols:
        hist = {p.session_date: (p, by_session[sym].get(p.session_date, [])) for p in prior}
        warm = by_session[sym].get(prior[-1].session_date, []) if prior else []
        out[sym] = build_context(sym, by_daily.get(sym, []), hist, warm,
                                 adv_lookback=adv_lookback)
    return out


async def load_session_bars(sm: async_sessionmaker[AsyncSession], feed: str,
                            session: SessionDay, symbols: Sequence[str], until: datetime
                            ) -> dict[str, list[Bar]]:
    async with sm() as s:
        rows = await sig_repo.minute_bars(s, feed, symbols, session.open_at, until)
    out: dict[str, list[Bar]] = defaultdict(list)
    for r in rows:
        out[r.symbol].append(_bar(r))
    return dict(out)


def last_completed_bar(now: datetime, session: SessionDay) -> datetime | None:
    """Start time of the latest 1-min bar that has closed (plus grace) within the session."""
    ref = now - BAR_GRACE
    bar_start = ref.replace(second=0, microsecond=0) - BAR_INTERVAL
    if bar_start < session.open_at:
        return None
    return min(bar_start, session.close_at - BAR_INTERVAL)


class DecisionService:
    def __init__(self, sm: async_sessionmaker[AsyncSession], *, feed: str,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.sm = sm
        self.feed = feed
        self.clock = clock
        self.contexts: dict[str, SymbolContext] = {}
        self.context_date: date | None = None
        self.last_bar_ts: datetime | None = None
        self.last: CycleResult | None = None
        self.last_bars: dict[str, list[Bar]] = {}
        self.last_session: SessionDay | None = None
        self.last_risk_error: str | None = None
        self.explain_queue: list[tuple[Any, SignalRecord, str]] = []
        self._last_ids: list[tuple[Any, SignalRecord]] = []

    async def ensure_contexts(self, calendar: MarketCalendar, session: SessionDay,
                              symbols: Sequence[str], cfg: TradingConfig) -> None:
        if self.context_date != session.session_date:
            self.contexts, self.context_date, self.last_bar_ts = {}, session.session_date, None
        missing = [s for s in symbols if s not in self.contexts]
        if missing:
            self.contexts.update(await load_contexts(self.sm, calendar, self.feed, session,
                                                     missing, cfg.universe.adv_lookback_days))
            log.info("decision_contexts_loaded", symbols=missing, session=str(session.session_date))

    async def cycle(self, calendar: MarketCalendar, universe: Sequence[str], cfg: TradingConfig,
                    config_version: int, block_reasons: Sequence[str], *,
                    risk: RiskAdapter | None = None, paper_mode_ok: bool = True,
                    stale_symbols: Sequence[str] = (), submit: Submitter | None = None,
                    extra_symbols: Sequence[str] = (), ai: AIService | None = None
                    ) -> CycleResult | None:
        """One cycle. ``extra_symbols`` (open positions) get bars loaded for exit rules."""
        now = self.clock()
        session = calendar.session(now.astimezone(ET).date())
        if session is None or not (session.open_at <= now < session.close_at + BAR_GRACE
                                   + BAR_INTERVAL):
            return None
        bar_ts = last_completed_bar(now, session)
        if bar_ts is None or (self.last_bar_ts is not None and bar_ts <= self.last_bar_ts):
            return None
        symbols = sorted(set(universe) | set(REFERENCE) | set(extra_symbols))
        await self.ensure_contexts(calendar, session, symbols, cfg)
        bars = await load_session_bars(self.sm, self.feed, session, symbols, bar_ts)
        news: list[Any] = []
        if cfg.news.enabled and universe:
            async with self.sm() as s:
                news = await recent_news(s, universe,
                                         now - timedelta(hours=cfg.news.lookback_hours))
        scores = {sym: news_factor([n.sentiment for n in news if sym in n.symbols])
                  for sym in universe}
        result = run_cycle(bar_ts, bars, self.contexts, session, universe, cfg, config_version,
                           news_scores=scores)
        risk_decisions: list[RiskDecision] = []
        self.last_risk_error = None
        if ai is not None and cfg.ai.mode == "VETO":
            for r in result.proposals:   # before the risk engine; the LLM can only remove
                r.llm_verdict, r.llm_call_id = await ai.veto(
                    r, [n for n in news if r.symbol in n.symbols][:10])
        if risk is not None and result.proposals:
            reqs = [(RiskProposal(r.symbol, "BUY", r.proposal.entry_ref_price,
                                  r.proposal.stop_loss, r.proposal.take_profit, cfg.ai.mode,
                                  r.llm_verdict),
                     r.features) for r in result.proposals if r.proposal is not None]
            gate = GateSnapshot(tuple(block_reasons), paper_mode_ok)
            risk_decisions = await risk.evaluate(reqs, cfg=cfg, regime=result.regime, gate=gate,
                                                 calendar=calendar, stale_symbols=stale_symbols)
            errors = [d.error for d in risk_decisions if d.error]
            self.last_risk_error = errors[0] if errors else None
        approved = await self._persist(result, session, cfg, config_version, block_reasons,
                                       risk_decisions, executing=submit is not None)
        if ai is not None and cfg.ai.mode in ("EXPLAIN", "VETO"):
            self.explain_queue = [(did, r, r.explanation or "") for did, r in self._last_ids]
        self.last_bar_ts, self.last = bar_ts, result
        self.last_bars, self.last_session = bars, session
        if submit is not None:
            for did, rd, rec in sorted(approved, key=lambda x: -x[2].score):
                code, reasons = await submit(did, rd, rec)
                if code != "ORDER_SUBMITTED" or reasons:
                    async with transaction(self.sm) as s:
                        await s.execute(update(AiDecision).where(AiDecision.id == did)
                                        .values(decision_result=code, block_reasons=reasons))
        if result.proposals:
            log.info("proposals", bar_ts=bar_ts.isoformat(), regime=result.regime.label,
                     proposals=[(r.symbol, r.strategy, r.score) for r in result.proposals],
                     blocked_by=list(block_reasons))
        return result

    async def _persist(self, result: CycleResult, session: SessionDay, cfg: TradingConfig,
                       config_version: int, block_reasons: Sequence[str],
                       risk_decisions: Sequence[RiskDecision] = (), *, executing: bool = False
                       ) -> list[tuple[Any, RiskDecision, SignalRecord]]:
        now = self.clock()
        signals, decisions, evaluations = [], [], []
        approved: list[tuple[Any, RiskDecision, SignalRecord]] = []
        self._last_ids = []
        blocks = list(block_reasons)
        risk_iter = iter(risk_decisions)
        for r in result.records:
            sid = uuid7()
            signals.append({
                "id": sid, "ts": now, "bar_ts": r.bar_ts, "session_date": session.session_date,
                "symbol": r.symbol, "strategy": r.strategy, "feed": self.feed,
                "features": r.features, "conditions": r.conditions,
                "factor_scores": r.factor_scores, "score": sig_repo.money(r.score),
                "regime": r.regime, "signals": r.signals, "action": r.action, "note": r.note,
                "config_version": config_version})
            p = r.proposal
            if p is None:
                continue
            did = uuid7()
            self._last_ids.append((did, r))
            rd = next(risk_iter, None)
            result_code, reasons = _outcome(rd, blocks, executing=executing)
            if rd is not None:
                evaluations.append(_evaluation_row(did, now, rd, p, blocks, config_version))
                if rd.approved and executing:
                    approved.append((did, rd, r))
            decisions.append({
                "id": did, "signal_id": sid, "ts": now, "symbol": r.symbol,
                "strategy": r.strategy, "market_price": sig_repo.money(r.features["price"]),
                "action": p.action, "confidence": sig_repo.money(p.confidence),
                "score": sig_repo.money(p.score), "signals": p.signals,
                "reasoning": r.explanation or "", "entry_ref_price": p.entry_ref_price,
                "stop_loss": p.stop_loss, "take_profit": p.take_profit,
                "risk_reward": sig_repo.money(p.risk_reward_ratio),
                "expected_return": sig_repo.money(p.expected_return),
                "estimated_risk": sig_repo.money(p.estimated_risk),
                "holding_period": p.holding_period, "ai_mode": cfg.ai.mode,
                "llm_verdict": r.llm_verdict, "llm_call_id": r.llm_call_id,
                "decision_result": result_code, "block_reasons": reasons,
                "risk_amount": rd.max_loss.quantize(sig_repo.CENTS) if rd else None,
                "model_version": p.model_version, "config_version": config_version})
        async with transaction(self.sm) as s:
            await sig_repo.insert_signals(s, signals, decisions)
            if evaluations:
                s.add_all(RiskEvaluation(**e) for e in evaluations)
        return approved

    def snapshot(self) -> dict[str, Any] | None:
        return self.last.summary() if self.last else None


def _outcome(rd: RiskDecision | None, blocks: list[str], *, executing: bool = False
             ) -> tuple[str, list[str]]:
    """decision_result and its reasons. Approved: ORDER_SUBMITTED (updated if it fails)."""
    if rd is None:
        return "BLOCKED", blocks or ["NO_RISK_ENGINE"]
    if rd.error:
        return "RISK_REJECTED", ["RISK_ENGINE_ERROR"]
    if "R25" in rd.failed:   # the LLM rejected (or failed): it may only remove trades
        return "VETOED", ["AI_VETO"]
    if rd.approved:
        return ("ORDER_SUBMITTED", []) if executing else ("BLOCKED", ["NO_BROKER"])
    if rd.only_gate_blocked:
        return "BLOCKED", blocks
    return "RISK_REJECTED", [c for c in rd.failed if c != "R1"] + (
        blocks if "R1" in rd.failed else [])


def _evaluation_row(decision_id: Any, now: datetime, rd: RiskDecision, p: Any,
                    blocks: list[str], config_version: int) -> dict[str, Any]:
    return {"id": uuid7(), "decision_id": decision_id, "ts": now, "approved": rd.approved,
            "checks": rd.checks_json(), "sizing": rd.sizing.to_json(), "qty": rd.sizing.qty,
            "position_value": rd.position_value.quantize(sig_repo.CENTS),
            "stop_price": rd.proposal.stop if rd.proposal else p.stop_loss,
            "take_profit_price": rd.proposal.target if rd.proposal else p.take_profit,
            "risk_reward": rd.risk_reward.quantize(sig_repo.CENTS) if rd.risk_reward else None,
            "max_loss": rd.max_loss.quantize(sig_repo.CENTS),
            "exposure_after": rd.exposure_after.quantize(sig_repo.CENTS),
            "open_positions_after": rd.open_positions_after,
            "daily_loss_worst_case_after": rd.daily_loss_worst_case_after.quantize(
                sig_repo.CENTS),
            "gate_blocks": blocks, "config_version": config_version}
