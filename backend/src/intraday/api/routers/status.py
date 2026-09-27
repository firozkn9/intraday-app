"""/status, /controls, /commands, /health (docs/design/05 §3, §4, §9)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
from intraday.api.security import passwords
from intraday.core.gate import BLOCK_SPECS
from intraday.core.runtime_config import parse_config
from intraday.core.timeutil import ET, utcnow
from intraday.db.models import EngineCommand
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import marketdata as md_repo
from intraday.db.repositories import settings as settings_repo
from intraday.db.repositories import users as users_repo

ENGINE_HEARTBEAT_STALE_SECONDS = 15

router = APIRouter(tags=["status"], dependencies=[Depends(read_limit)])
health_router = APIRouter(tags=["health"])


def _command_json(c: EngineCommand) -> dict[str, Any]:
    return {"id": str(c.id), "ts": c.ts.isoformat(), "command": c.command, "params": c.params,
            "status": c.status, "acked_at": c.acked_at.isoformat() if c.acked_at else None,
            "completed_at": c.completed_at.isoformat() if c.completed_at else None,
            "result": c.result}


async def _status(ctx_trading_mode: str, db: AsyncSession) -> dict[str, Any]:
    state = await engine_repo.get_state(db)
    active = await settings_repo.get_active(db)
    cfg = parse_config(active.config) if active else None
    now = utcnow()
    age = (now - state.heartbeat_at).total_seconds() if state and state.heartbeat_at else None
    online = age is not None and age <= ENGINE_HEARTBEAT_STALE_SECONDS
    blocks = list(state.blocks) if state else []
    if not online:
        blocks = [{"reason": "ENGINE_OFFLINE", "since": None, "latched": False,
                   "detail": "Engine heartbeat missing; brackets at the broker protect "
                             "any open positions."}, *blocks]
    return {
        "trading_mode": ctx_trading_mode,
        "status": state.status if state else "PAUSED",
        "engine_online": online,
        "engine_heartbeat_age_s": round(age, 1) if age is not None else None,
        "blocks": blocks,
        "latched": list(state.latched) if state else [],
        "trading_enabled": bool(state and state.trading_enabled),
        "running_config_version": state.config_version if state else None,
        "active_config_version": active.version if active else None,
        "pending_restart": (state.details or {}).get("pending_restart", []) if state else [],
        "paper_guard": (state.details or {}).get("paper_guard") if state else None,
        "ai_mode": cfg.ai.mode if cfg else None,
        "data_feed": cfg.data.feed if cfg else None,
        "market": await _market(db, state.details if state else None),
        "engine_version": state.engine_version if state else None,
    }


async def _market(db: AsyncSession, details: dict[str, Any] | None) -> dict[str, Any]:
    """Session status from the DB calendar (works while the engine is offline)."""
    now = utcnow()
    today = now.astimezone(ET).date()
    cal = await md_repo.load_calendar(db, today - timedelta(days=7), today + timedelta(days=30))
    out = cal.status(now)
    engine_market = (details or {}).get("market") or {}
    if "window" in engine_market:
        out["window"] = engine_market["window"]
    md = (details or {}).get("market_data") or {}
    out["data"] = {"enabled": md.get("enabled", False), "reason": md.get("reason"),
                   "feed": md.get("feed"), "stream": (md.get("stream") or {}).get("state"),
                   "subscribed": (md.get("plan") or {}).get("used"),
                   "stale_symbols": (md.get("health") or {}).get("stale_symbols", [])}
    return out


@router.get("/status")
async def status(principal: CurrentPrincipal, ctx: Ctx, db: DB) -> dict[str, Any]:
    return await _status(ctx.env.trading_mode, db)


async def _enqueue(command: str, principal: Admin, request: Request, ctx: Ctx, db: DB,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
    cmd = await engine_repo.enqueue_command(db, command=command, user_id=principal.user.id,
                                            params=params)
    await users_repo.audit(db, action=f"COMMAND_{command}", user_id=principal.user.id,
                           target=f"command:{cmd.id}", after=params or None,
                           ip=client_ip(request), user_agent=user_agent(request))
    await ctx.publish("commands:new", {"id": str(cmd.id)})
    return {"command_id": str(cmd.id), "status": cmd.status}


@router.post("/controls/start", status_code=202, dependencies=[Depends(mutation_limit)])
async def start(principal: Admin, request: Request, ctx: Ctx, db: DB) -> dict[str, Any]:
    state = await engine_repo.get_state(db)
    if state is not None and state.latched:
        raise Problem(409, "Reset required before starting",
                      f"Latched: {', '.join(state.latched)}")
    if state is not None and any(b.get("reason") == "CONFIG_ERROR" for b in state.blocks):
        raise Problem(409, "Engine configuration error",
                      BLOCK_SPECS["CONFIG_ERROR"].description)
    return await _enqueue("START", principal, request, ctx, db)


@router.post("/controls/pause", status_code=202, dependencies=[Depends(mutation_limit)])
async def pause(principal: Admin, request: Request, ctx: Ctx, db: DB) -> dict[str, Any]:
    return await _enqueue("PAUSE", principal, request, ctx, db)


class EmergencyStopIn(BaseModel):
    confirm: str
    close_positions: bool | None = None


@router.post("/controls/emergency-stop", status_code=202, dependencies=[Depends(mutation_limit)])
async def emergency_stop(body: EmergencyStopIn, principal: Admin, request: Request, ctx: Ctx,
                         db: DB) -> dict[str, Any]:
    """Latch EMERGENCY_STOP, cancel entries and (by default) close every position."""
    if body.confirm != "EMERGENCY STOP":
        raise Problem(422, "Confirmation required", 'Type "EMERGENCY STOP" to confirm.')
    params: dict[str, Any] = {}
    if body.close_positions is not None:
        params["close_positions"] = body.close_positions
    return await _enqueue("EMERGENCY_STOP", principal, request, ctx, db, params)


class CloseAllIn(BaseModel):
    confirm: str


@router.post("/controls/close-all", status_code=202, dependencies=[Depends(mutation_limit)])
async def close_all(body: CloseAllIn, principal: Admin, request: Request, ctx: Ctx, db: DB
                    ) -> dict[str, Any]:
    """Cancel open orders and flatten. Does not latch; trading stays paused until START."""
    if body.confirm != "CLOSE ALL":
        raise Problem(422, "Confirmation required", 'Type "CLOSE ALL" to confirm.')
    return await _enqueue("CLOSE_ALL", principal, request, ctx, db)


@router.post("/controls/reconcile-now", status_code=202, dependencies=[Depends(mutation_limit)])
async def reconcile_now(principal: Admin, request: Request, ctx: Ctx, db: DB
                        ) -> dict[str, Any]:
    return await _enqueue("RECONCILE_NOW", principal, request, ctx, db)


class ResetIn(BaseModel):
    confirm: str
    password: str = Field(max_length=256)


class ResetLatchIn(ResetIn):
    latch: Literal["EMERGENCY_STOP", "RECONCILE_MISMATCH", "RISK_ENGINE_ERROR"]


async def _reset(command: str, latch: str, body: ResetIn, principal: Admin, request: Request,
                 ctx: Ctx, db: DB) -> dict[str, Any]:
    if body.confirm != "RESET":
        raise Problem(422, "Confirmation required", 'Type "RESET" to confirm.')
    if not passwords.verify_password(principal.user.password_hash, body.password):
        await users_repo.audit(db, action=f"COMMAND_{command}", user_id=principal.user.id,
                               target=latch, ip=client_ip(request),
                               user_agent=user_agent(request), result="DENIED")
        raise Problem(403, "Password is incorrect")
    state = await engine_repo.get_state(db)
    if state is None or latch not in (state.latched or []):
        raise Problem(409, f"{latch} is not active")
    cmd = await engine_repo.enqueue_command(db, command=command, user_id=principal.user.id,
                                            params={"latch": latch})
    await users_repo.audit(db, action=f"COMMAND_{command}", user_id=principal.user.id,
                           target=f"command:{cmd.id}", after={"latch": latch},
                           ip=client_ip(request), user_agent=user_agent(request))
    await ctx.publish("commands:new", {"id": str(cmd.id)})
    return {"command_id": str(cmd.id), "status": cmd.status}


@router.post("/controls/reset-daily-lock", status_code=202,
             dependencies=[Depends(mutation_limit)])
async def reset_daily_lock(body: ResetIn, principal: Admin, request: Request, ctx: Ctx,
                           db: DB) -> dict[str, Any]:
    """Clear the daily-loss latch. Trading stays paused until START (SPEC §6)."""
    return await _reset("RESET_DAILY_LOCK", "DAILY_LOSS_LIMIT", body, principal, request, ctx,
                        db)


@router.post("/controls/reset-latch", status_code=202, dependencies=[Depends(mutation_limit)])
async def reset_latch(body: ResetLatchIn, principal: Admin, request: Request, ctx: Ctx,
                      db: DB) -> dict[str, Any]:
    return await _reset("RESET_LATCH", body.latch, body, principal, request, ctx, db)


@router.get("/commands")
async def list_commands(principal: CurrentPrincipal, db: DB,
                        limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    return [_command_json(c) for c in await engine_repo.recent_commands(db, limit)]


@router.get("/commands/{command_id}")
async def get_command(command_id: uuid.UUID, principal: CurrentPrincipal, db: DB
                      ) -> dict[str, Any]:
    cmd = await engine_repo.get_command(db, command_id)
    if cmd is None:
        raise Problem(404, "Command not found")
    return _command_json(cmd)


@health_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@health_router.get("/health/ready")
async def ready(ctx: Ctx) -> dict[str, Any]:
    checks: dict[str, str] = {}
    try:
        async with ctx.sessionmaker() as s:
            await s.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    if ctx.redis is None:
        checks["redis"] = "not configured"
    else:
        try:
            await ctx.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"degraded: {type(exc).__name__}"
    if checks["database"] != "ok":
        raise Problem(503, "Not ready", extra={"checks": checks})
    return {"status": "ready", "checks": checks}
