"""/system/health, /metrics, /logs, /audit, /alerts, /news (docs/design/05 §6, §9)."""

from __future__ import annotations

import os
import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, text

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
from intraday.core.timeutil import utcnow
from intraday.db.models import (
    Alert,
    AuditLog,
    LlmCall,
    NewsItem,
    Order,
    RiskEvent,
    SystemEvent,
)
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import trading as trading_repo
from intraday.db.repositories import users as users_repo

router = APIRouter(tags=["system"], dependencies=[Depends(read_limit)])
internal_router = APIRouter(tags=["system"])
BACKUP_STATUS = Path(os.environ.get("BACKUP_STATUS_FILE", "/backups/last_success"))


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * len(v)))], 1)


async def _order_stats(db: DB, since: datetime) -> dict[str, Any]:
    rows = list((await db.execute(select(Order).where(Order.created_at >= since,
                                                      Order.parent_order_id.is_(None))))
                .scalars())
    lat = [(o.acked_at - o.submitted_at).total_seconds() * 1000 for o in rows
           if o.acked_at and o.submitted_at and o.acked_at >= o.submitted_at]
    rejected = sum(1 for o in rows if o.status in ("REJECTED", "FAILED"))
    return {"orders_24h": len(rows), "ack_latency_ms_p50": _pct(lat, 0.5),
            "ack_latency_ms_p95": _pct(lat, 0.95),
            "rejection_rate": round(rejected / len(rows), 4) if rows else None}


def _last_backup() -> str | None:
    try:
        return BACKUP_STATUS.read_text(encoding="utf-8").strip()
    except OSError:
        return None


@router.get("/system/health")
async def system_health(principal: CurrentPrincipal, ctx: Ctx, db: DB) -> dict[str, Any]:
    now = utcnow()
    t0 = time.perf_counter()
    await db.execute(text("SELECT 1"))
    db_ms = round((time.perf_counter() - t0) * 1000, 2)
    redis_state = "not configured"
    if ctx.redis is not None:
        try:
            await ctx.redis.ping()
            redis_state = "ok"
        except Exception as exc:
            redis_state = f"degraded: {type(exc).__name__}"
    state = await engine_repo.get_state(db)
    details = state.details if state else {}
    age = (now - state.heartbeat_at).total_seconds() if state and state.heartbeat_at else None
    ai_calls = list((await db.execute(select(LlmCall).order_by(LlmCall.ts.desc()).limit(20)))
                    .scalars())
    disk = shutil.disk_usage(Path.cwd().anchor or "/")
    backup = _last_backup()
    return {
        "engine": {"heartbeat_age_s": round(age, 1) if age is not None else None,
                   "status": state.status if state else None,
                   "blocks": state.blocks if state else []},
        "broker": details.get("broker"), "market_data": details.get("market_data"),
        "ai": {**(details.get("ai") or {}),
               "last_latency_ms": ai_calls[0].latency_ms if ai_calls else None,
               "recent_invalid": sum(1 for c in ai_calls if not c.valid)},
        "database": {"latency_ms": db_ms}, "redis": redis_state,
        "orders": await _order_stats(db, now - timedelta(days=1)),
        "disk": {"free_gb": round(disk.free / 1e9, 2),
                 "used_pct": round(disk.used / disk.total * 100, 1)},
        "last_backup": backup,
    }


@internal_router.get("/metrics", response_class=PlainTextResponse)
async def metrics(db: DB) -> str:
    """Prometheus text. Caddy does not proxy /metrics (internal network only)."""
    state = await engine_repo.get_state(db)
    now = utcnow()
    age = (now - state.heartbeat_at).total_seconds() if state and state.heartbeat_at else -1
    open_alerts = (await db.execute(select(func.count()).select_from(Alert).where(
        Alert.acknowledged_at.is_(None)))).scalar_one()
    live = len(await trading_repo.live_positions(db))
    lines = [
        "# TYPE intraday_engine_heartbeat_age_seconds gauge",
        f"intraday_engine_heartbeat_age_seconds {age:.1f}",
        "# TYPE intraday_open_alerts gauge", f"intraday_open_alerts {open_alerts}",
        "# TYPE intraday_live_positions gauge", f"intraday_live_positions {live}",
        "# TYPE intraday_trading_enabled gauge",
        f"intraday_trading_enabled {int(bool(state and state.trading_enabled))}",
    ]
    return "\n".join(lines) + "\n"


@router.get("/logs")
async def logs(principal: CurrentPrincipal, db: DB, component: str | None = None,
               severity: str | None = None, date_from: datetime | None = Query(None, alias="from"),
               date_to: datetime | None = Query(None, alias="to"), q: str | None = None,
               limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    se = select(SystemEvent).order_by(SystemEvent.ts.desc()).limit(limit)
    re_ = select(RiskEvent).order_by(RiskEvent.ts.desc()).limit(limit)
    if date_from:
        se, re_ = se.where(SystemEvent.ts >= date_from), re_.where(RiskEvent.ts >= date_from)
    if date_to:
        se, re_ = se.where(SystemEvent.ts <= date_to), re_.where(RiskEvent.ts <= date_to)
    if severity:
        se, re_ = se.where(SystemEvent.severity == severity), re_.where(
            RiskEvent.severity == severity)
    if component:
        se = se.where(SystemEvent.component == component)
    out: list[dict[str, Any]] = [
           {"ts": e.ts.isoformat(), "kind": "system", "component": e.component, "type": e.type,
            "severity": e.severity, "message": e.message, "details": e.details}
           for e in (await db.execute(se)).scalars()]
    if component in (None, "risk"):
        out += [{"ts": e.ts.isoformat(), "kind": "risk", "component": "risk", "type": e.type,
                 "severity": e.severity, "message": e.type.replace("_", " ").lower()
                 + (f" ({e.symbol})" if e.symbol else ""), "details": e.details}
                for e in (await db.execute(re_)).scalars()]
    if q:
        ql = q.lower()
        out = [e for e in out if ql in e["message"].lower() or ql in e["type"].lower()]
    return sorted(out, key=lambda e: str(e["ts"]), reverse=True)[:limit]


@router.get("/audit")
async def audit(principal: Admin, db: DB, action: str | None = None,
                date_from: datetime | None = Query(None, alias="from"),
                date_to: datetime | None = Query(None, alias="to"),
                limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    stmt = select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if date_from:
        stmt = stmt.where(AuditLog.ts >= date_from)
    if date_to:
        stmt = stmt.where(AuditLog.ts <= date_to)
    return [{"ts": a.ts.isoformat(), "action": a.action,
             "user_id": str(a.user_id) if a.user_id else None, "target": a.target,
             "result": a.result, "ip": a.ip} for a in (await db.execute(stmt)).scalars()]


def _alert_json(a: Alert) -> dict[str, Any]:
    return {"id": str(a.id), "ts": a.ts.isoformat(), "severity": a.severity, "type": a.type,
            "message": a.message, "count": a.count, "last_seen_at": a.last_seen_at.isoformat(),
            "acknowledged_at": a.acknowledged_at.isoformat() if a.acknowledged_at else None,
            "channels_sent": a.channels_sent}


@router.get("/alerts")
async def alerts(principal: CurrentPrincipal, db: DB,
                 state: Literal["open", "all"] = "open",
                 limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    stmt = select(Alert).order_by(Alert.last_seen_at.desc()).limit(limit)
    if state == "open":
        stmt = stmt.where(Alert.acknowledged_at.is_(None))
    return [_alert_json(a) for a in (await db.execute(stmt)).scalars()]


@router.post("/alerts/{alert_id}/ack", dependencies=[Depends(mutation_limit)])
async def ack_alert(alert_id: uuid.UUID, principal: Admin, request: Request, db: DB
                    ) -> dict[str, Any]:
    a = await db.get(Alert, alert_id)
    if a is None:
        raise Problem(404, "Alert not found")
    if a.acknowledged_at is None:
        a.acknowledged_at, a.acknowledged_by = utcnow(), principal.user.id
        await users_repo.audit(db, action="ALERT_ACK", user_id=principal.user.id,
                               target=f"alert:{a.id}", ip=client_ip(request),
                               user_agent=user_agent(request))
    return _alert_json(a)


@router.get("/news")
async def news(principal: CurrentPrincipal, db: DB, symbol: str | None = None,
               since: datetime | None = None, limit: int = Query(100, ge=1, le=500)
               ) -> list[dict[str, Any]]:
    stmt = select(NewsItem).order_by(NewsItem.published_at.desc()).limit(limit)
    if symbol:
        stmt = stmt.where(NewsItem.symbols.contains([symbol.upper()]))
    if since:
        stmt = stmt.where(NewsItem.published_at >= since)
    return [{"id": str(n.id), "published_at": n.published_at.isoformat(), "symbols": n.symbols,
             "headline": n.headline, "source": n.source, "url": n.url,
             "event_tags": n.event_tags, "sentiment": n.sentiment,
             "sentiment_source": n.sentiment_source} for n in (await db.execute(stmt)).scalars()]

