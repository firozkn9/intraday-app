"""/backtests (docs/design/05 §8). Runs execute in a separate worker process."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError
from sqlalchemy import select

from intraday.api.deps import (
    DB,
    Admin,
    Ctx,
    CurrentPrincipal,
    client_ip,
    mutation_limit,
    read_limit,
    user_agent,
)
from intraday.api.errors import Problem
from intraday.backtest.job import BacktestParams, coverage_gaps, effective_config
from intraday.core.runtime_config import ConfigError, parse_config
from intraday.core.timeutil import utcnow
from intraday.db.models import BacktestEquity, BacktestRun, BacktestTrade
from intraday.db.repositories import settings as settings_repo
from intraday.db.repositories import users as users_repo

router = APIRouter(prefix="/backtests", tags=["backtests"], dependencies=[Depends(read_limit)])

ACTIVE = ("QUEUED", "RUNNING")
STALE_AFTER = timedelta(hours=6)


def _run_json(r: BacktestRun, *, full: bool = False) -> dict[str, Any]:
    summary = {split: {k: m.get(k) for k in ("total_return_pct", "trades", "win_rate_pct",
                                             "profit_factor", "sharpe", "max_drawdown_pct")}
               for split, m in (r.metrics or {}).items()}
    out: dict[str, Any] = {
        "id": str(r.id), "created_at": r.created_at.isoformat(), "status": r.status,
        "feed": r.feed, "params": r.params, "summary": summary, "error": r.error,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None}
    if full:
        out["metrics"] = r.metrics
        out["config_snapshot"] = r.config_snapshot
    return out


@router.post("", status_code=202, dependencies=[Depends(mutation_limit)])
async def create(body: dict[str, Any], principal: Admin, request: Request, ctx: Ctx, db: DB
                 ) -> dict[str, Any]:
    try:
        params = BacktestParams.model_validate(body)
    except ValidationError as exc:
        raise Problem(422, "Invalid backtest parameters", errors=[
            {"key": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
            for e in exc.errors()]) from exc
    active = await settings_repo.get_active(db)
    base = parse_config(active.config) if active else ctx.defaults
    try:
        cfg = effective_config(base, params)
    except ConfigError as exc:
        raise Problem(422, "Invalid config overrides", errors=[
            {"key": e.key, "message": e.message} for e in exc.errors]) from exc

    running = (await db.execute(select(BacktestRun).where(
        BacktestRun.status.in_(ACTIVE)))).scalars().all()
    if any(utcnow() - r.created_at < STALE_AFTER for r in running):
        raise Problem(409, "A backtest is already running", "Wait for it or cancel it.")

    gaps = await coverage_gaps(ctx.sessionmaker, params, cfg.data.feed)
    if gaps:
        raise Problem(422, "Not enough stored data for these dates",
                      errors=[{"key": "data", "message": g} for g in gaps])

    run = BacktestRun(created_by=principal.user.id, status="QUEUED",
                      params=params.model_dump(mode="json"), config_snapshot=cfg.to_json(),
                      feed=cfg.data.feed)
    db.add(run)
    await db.flush()
    await users_repo.audit(db, action="BACKTEST_CREATE", user_id=principal.user.id,
                           target=f"backtest:{run.id}", after=run.params,
                           ip=client_ip(request), user_agent=user_agent(request))
    run_id = run.id
    await db.commit()   # the worker must see the row
    if ctx.launch_backtest is not None:
        ctx.launch_backtest(run_id)
    return {"id": str(run_id), "status": "QUEUED"}


@router.get("")
async def list_runs(principal: CurrentPrincipal, db: DB,
                    limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(BacktestRun).order_by(BacktestRun.created_at.desc())
                             .limit(limit))).scalars().all()
    return [_run_json(r) for r in rows]


async def _get(db: DB, run_id: uuid.UUID) -> BacktestRun:
    run = await db.get(BacktestRun, run_id)
    if run is None:
        raise Problem(404, "Backtest not found")
    return run


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    return _run_json(await _get(db, run_id), full=True)


@router.get("/{run_id}/trades")
async def trades(run_id: uuid.UUID, principal: CurrentPrincipal, db: DB,
                 split: Literal["TRAIN", "VALIDATION", "TEST"] | None = None,
                 limit: int = Query(1000, ge=1, le=5000)) -> list[dict[str, Any]]:
    await _get(db, run_id)
    stmt = select(BacktestTrade).where(BacktestTrade.run_id == run_id)
    if split:
        stmt = stmt.where(BacktestTrade.split == split)
    rows = (await db.execute(stmt.order_by(BacktestTrade.entry_time).limit(limit))).scalars()
    return [{"split": t.split, "session_date": str(t.session_date), "symbol": t.symbol,
             "strategy": t.strategy, "qty": t.qty, "entry_price": str(t.avg_entry_price),
             "entry_time": t.entry_time.isoformat(), "exit_price": str(t.avg_exit_price),
             "exit_time": t.exit_time.isoformat(), "exit_reason": t.exit_reason,
             "net_pnl": str(t.net_pnl), "fees": str(t.fees), "return_pct": str(t.return_pct),
             "holding_seconds": t.holding_seconds} for t in rows]


@router.get("/{run_id}/equity")
async def equity(run_id: uuid.UUID, principal: CurrentPrincipal, db: DB
                 ) -> list[dict[str, Any]]:
    await _get(db, run_id)
    rows = (await db.execute(select(BacktestEquity).where(BacktestEquity.run_id == run_id)
                             .order_by(BacktestEquity.ts))).scalars()
    return [{"ts": e.ts.isoformat(), "equity": str(e.equity), "drawdown": str(e.drawdown)}
            for e in rows]


@router.post("/{run_id}/cancel", dependencies=[Depends(mutation_limit)])
async def cancel(run_id: uuid.UUID, principal: Admin, request: Request, db: DB
                 ) -> dict[str, Any]:
    run = await _get(db, run_id)
    if run.status not in ACTIVE:
        raise Problem(409, f"Backtest is {run.status}")
    run.status = "CANCELED"
    await users_repo.audit(db, action="BACKTEST_CANCEL", user_id=principal.user.id,
                           target=f"backtest:{run_id}", ip=client_ip(request),
                           user_agent=user_agent(request))
    return {"id": str(run_id), "status": "CANCELED"}
