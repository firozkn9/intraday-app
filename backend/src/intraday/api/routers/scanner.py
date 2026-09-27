"""/scanner (docs/design/05 §6): latest universe scan with every filter's reason."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

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
from intraday.db.models import UniverseScan
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import marketdata as md_repo
from intraday.db.repositories import users as users_repo

router = APIRouter(prefix="/scanner", tags=["scanner"], dependencies=[Depends(read_limit)])


def _row(r: UniverseScan) -> dict[str, Any]:
    return {"symbol": r.symbol, "passed": r.passed, "reasons": r.reasons,
            "failed": [k for k, v in r.reasons.items() if not v.get("passed")],
            "metrics": r.metrics, "ts": r.ts.isoformat()}


@router.get("")
async def latest(principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    rows = await md_repo.latest_scan(db)
    if not rows:
        return {"scan": None, "results": []}
    first = rows[0]
    return {
        "scan": {"scan_id": str(first.scan_id), "ts": first.ts.isoformat(),
                 "session_date": first.session_date.isoformat(), "feed": first.feed,
                 "config_version": first.config_version,
                 "passed": sum(1 for r in rows if r.passed),
                 "failed": sum(1 for r in rows if not r.passed)},
        "results": [_row(r) for r in rows],
    }


@router.get("/history")
async def history(principal: CurrentPrincipal, db: DB,
                  symbol: str = Query(min_length=1, max_length=8),
                  limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
    return [_row(r) for r in await md_repo.scan_history(db, symbol.upper(), limit)]


@router.post("/run", status_code=202, dependencies=[Depends(mutation_limit)])
async def run(principal: Admin, request: Request, ctx: Ctx, db: DB) -> dict[str, Any]:
    cmd = await engine_repo.enqueue_command(db, command="SCAN", user_id=principal.user.id)
    await users_repo.audit(db, action="COMMAND_SCAN", user_id=principal.user.id,
                           target=f"command:{cmd.id}", ip=client_ip(request),
                           user_agent=user_agent(request))
    await ctx.publish("commands:new", {"id": str(cmd.id)})
    return {"command_id": str(cmd.id), "status": cmd.status}
