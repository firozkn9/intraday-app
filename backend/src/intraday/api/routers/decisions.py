"""/signals, /decisions, /regime (docs/design/05 §6). Read-only views of the audit chain."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from intraday.api.deps import DB, CurrentPrincipal, read_limit
from intraday.api.errors import Problem
from intraday.db.models import AiDecision, TradingSignal
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import signals as sig_repo

router = APIRouter(tags=["decisions"], dependencies=[Depends(read_limit)])


def _signal_json(s: TradingSignal) -> dict[str, Any]:
    return {"id": str(s.id), "bar_ts": s.bar_ts.isoformat(), "session_date": str(s.session_date),
            "symbol": s.symbol, "strategy": s.strategy, "feed": s.feed, "action": s.action,
            "score": float(s.score), "regime": s.regime, "signals": s.signals, "note": s.note,
            "conditions": s.conditions, "factor_scores": s.factor_scores,
            "features": s.features, "config_version": s.config_version}


def _decision_json(d: AiDecision) -> dict[str, Any]:
    return {"id": str(d.id), "ts": d.ts.isoformat(), "symbol": d.symbol,
            "strategy": d.strategy, "action": d.action, "confidence": float(d.confidence),
            "score": float(d.score), "market_price": str(d.market_price),
            "entry_ref_price": str(d.entry_ref_price), "stop_loss": str(d.stop_loss),
            "take_profit": str(d.take_profit), "risk_reward": float(d.risk_reward),
            "signals": d.signals, "reasoning": d.reasoning, "reasoning_llm": d.reasoning_llm,
            "ai_mode": d.ai_mode, "llm_verdict": d.llm_verdict,
            "decision_result": d.decision_result, "block_reasons": d.block_reasons,
            "risk_amount": str(d.risk_amount) if d.risk_amount is not None else None,
            "model_version": d.model_version, "config_version": d.config_version}


@router.get("/signals")
async def list_signals(principal: CurrentPrincipal, db: DB,
                       symbol: str | None = Query(None, max_length=8),
                       session_date: date | None = Query(None, alias="date"),
                       strategy: str | None = Query(None, max_length=32),
                       action: Literal["BUY", "NO_TRADE"] | None = None,
                       limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    rows = await sig_repo.list_signals(db, symbol=symbol.upper() if symbol else None,
                                       session_date=session_date, strategy=strategy,
                                       action=action, limit=limit)
    return [_signal_json(r) for r in rows]


@router.get("/decisions")
async def list_decisions(principal: CurrentPrincipal, db: DB,
                         symbol: str | None = Query(None, max_length=8),
                         start: datetime | None = None, end: datetime | None = None,
                         result: str | None = Query(None, max_length=16),
                         strategy: str | None = Query(None, max_length=32),
                         limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    rows = await sig_repo.list_decisions(db, symbol=symbol.upper() if symbol else None,
                                         start=start, end=end, result=result,
                                         strategy=strategy, limit=limit)
    return [_decision_json(r) for r in rows]


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: uuid.UUID, principal: CurrentPrincipal, db: DB
                       ) -> dict[str, Any]:
    found = await sig_repo.get_decision(db, decision_id)
    if found is None:
        raise Problem(404, "Decision not found")
    dec, sig, risk = found
    return {**_decision_json(dec), "signal": _signal_json(sig),
            "risk": None if risk is None else {
                "approved": risk.approved, "checks": risk.checks, "sizing": risk.sizing,
                "qty": risk.qty, "position_value": str(risk.position_value),
                "max_loss": str(risk.max_loss), "exposure_after": str(risk.exposure_after),
                "daily_loss_worst_case_after": str(risk.daily_loss_worst_case_after),
                "gate_blocks": risk.gate_blocks}}


@router.get("/regime")
async def regime(principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    state = await engine_repo.get_state(db)
    snap = ((state.details or {}).get("signals") or {}) if state else {}
    return {"bar_ts": snap.get("bar_ts"), "regime": snap.get("regime"),
            "evaluated": snap.get("evaluated", []), "proposals": snap.get("proposals", [])}
