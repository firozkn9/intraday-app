"""Engine state singleton, engine commands, and system events."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from intraday.db.models import Alert, EngineCommand, EngineState, RiskEvent, SystemEvent

ENGINE_STATE_ID = 1


async def get_state(session: AsyncSession) -> EngineState | None:
    return await session.get(EngineState, ENGINE_STATE_ID)


async def upsert_state(session: AsyncSession, **values: Any) -> None:
    stmt = insert(EngineState).values(id=ENGINE_STATE_ID, **values)
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=values)
    await session.execute(stmt)


async def ensure_state(session: AsyncSession, *, status: str) -> EngineState:
    await session.execute(
        insert(EngineState).values(id=ENGINE_STATE_ID, status=status, trading_enabled=False)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    state = await get_state(session)
    if state is None:  # pragma: no cover - the row was just inserted
        raise RuntimeError("engine_state row missing after insert")
    return state


async def enqueue_command(session: AsyncSession, *, command: str,
                          user_id: uuid.UUID | None, params: dict[str, Any] | None = None
                          ) -> EngineCommand:
    cmd = EngineCommand(command=command, user_id=user_id, params=params or {}, status="PENDING")
    session.add(cmd)
    await session.flush()
    return cmd


async def get_command(session: AsyncSession, command_id: uuid.UUID) -> EngineCommand | None:
    return await session.get(EngineCommand, command_id)


async def recent_commands(session: AsyncSession, limit: int = 20) -> list[EngineCommand]:
    res = await session.execute(select(EngineCommand).order_by(EngineCommand.ts.desc())
                                .limit(limit))
    return list(res.scalars())


async def claim_pending_commands(session: AsyncSession, now: datetime, limit: int = 10
                                 ) -> list[EngineCommand]:
    """Lock and ACK pending commands (oldest first). Call inside a transaction."""
    res = await session.execute(
        select(EngineCommand).where(EngineCommand.status == "PENDING")
        .order_by(EngineCommand.ts).limit(limit).with_for_update(skip_locked=True)
    )
    cmds = list(res.scalars())
    for c in cmds:
        c.status = "ACKED"
        c.acked_at = now
    await session.flush()
    return cmds


async def complete_command(session: AsyncSession, command_id: uuid.UUID, *, ok: bool,
                           result: dict[str, Any], now: datetime) -> None:
    await session.execute(
        update(EngineCommand).where(EngineCommand.id == command_id)
        .values(status="DONE" if ok else "FAILED", result=result, completed_at=now)
    )


async def system_event(session: AsyncSession, *, component: str, type: str, severity: str,
                       message: str, details: dict[str, Any] | None = None) -> None:
    session.add(SystemEvent(component=component, type=type, severity=severity, message=message,
                            details=details or {}))
    await session.flush()


async def raise_alert(session: AsyncSession, *, severity: str, type: str, message: str,
                      dedupe_key: str | None = None) -> None:
    """Insert an alert, or bump the count of an unacknowledged one with the same key."""
    if dedupe_key:
        existing = (await session.execute(
            select(Alert).where(Alert.dedupe_key == dedupe_key,
                                Alert.acknowledged_at.is_(None)))).scalar_one_or_none()
        if existing is not None:
            existing.count += 1
            existing.last_seen_at = func.now()
            existing.message = message
            await session.flush()
            return
    session.add(Alert(severity=severity, type=type, message=message, dedupe_key=dedupe_key))
    await session.flush()


async def risk_event(session: AsyncSession, *, type: str, severity: str,
                     symbol: str | None = None, details: dict[str, Any] | None = None) -> None:
    session.add(RiskEvent(type=type, severity=severity, symbol=symbol, details=details or {}))
    await session.flush()
