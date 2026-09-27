"""/settings: versioned runtime configuration (docs/design/05 §7, 06 §4)."""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from intraday.api.context import AppContext
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
from intraday.core.runtime_config import (
    FIELD_SPECS,
    Change,
    ConfigError,
    FieldError,
    TradingConfig,
    diff_configs,
    hard_limit_violations,
    locked_violations,
    parse_config,
)
from intraday.db.models import SettingsVersion, User
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import settings as settings_repo
from intraday.db.repositories import users as users_repo

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(read_limit)])

MAX_IMPORT_BYTES = 64 * 1024


class ConfigIn(BaseModel):
    config: dict[str, Any]


class SaveIn(ConfigIn):
    base_version: int
    comment: str = Field(default="", max_length=500)


class RollbackIn(BaseModel):
    version: int
    base_version: int
    comment: str = Field(default="", max_length=500)


class ImportIn(BaseModel):
    yaml: str = Field(max_length=MAX_IMPORT_BYTES)
    base_version: int
    comment: str = Field(default="", max_length=500)


def _errors_json(errors: list[FieldError]) -> list[dict[str, str]]:
    return [{"key": e.key, "message": e.message} for e in errors]


async def _active(db: AsyncSession, ctx: AppContext) -> SettingsVersion:
    active = await settings_repo.get_active(db)
    if active is None:
        active = await settings_repo.ensure_initial(db, ctx.defaults)
    return active


async def _engine_paused(db: AsyncSession) -> bool:
    state = await engine_repo.get_state(db)
    return state is None or not state.trading_enabled


async def _evaluate(db: AsyncSession, active: SettingsVersion, data: dict[str, Any]
                    ) -> tuple[TradingConfig | None, list[FieldError], list[Change]]:
    """Validate a proposed config against the model, locks, and hard limits."""
    current = parse_config(active.config)
    try:
        proposed = parse_config(data)
    except ConfigError as exc:
        return None, exc.errors, []
    changes = diff_configs(current, proposed)
    equity = await settings_repo.reference_equity(db, current, proposed)
    errors = locked_violations(changes) + hard_limit_violations(proposed, equity)
    return proposed, errors, changes


def _evaluation_json(errors: list[FieldError], changes: list[Change], paused: bool
                     ) -> dict[str, Any]:
    requires_pause = any(c.requires_pause for c in changes)
    return {
        "valid": not errors,
        "errors": _errors_json(errors),
        "changes": [c.to_json() for c in changes],
        "requires_pause": requires_pause,
        "blocked_by_running": requires_pause and not paused,
        "pending_restart": [c.key for c in changes if c.policy == "RESTART"],
    }


def _version_json(v: SettingsVersion, author: str | None = None) -> dict[str, Any]:
    return {"version": v.version, "created_at": v.created_at.isoformat(),
            "created_by": author, "comment": v.comment, "is_active": v.is_active}


async def _author(db: AsyncSession, v: SettingsVersion) -> str | None:
    if v.created_by is None:
        return None
    user = await db.get(User, v.created_by)
    return user.email if user else None


@router.get("")
async def get_settings(principal: CurrentPrincipal, ctx: Ctx, db: DB) -> dict[str, Any]:
    active = await _active(db, ctx)
    state = await engine_repo.get_state(db)
    return {
        **_version_json(active, await _author(db, active)),
        "config": active.config,
        "fields": [spec.to_json() for spec in FIELD_SPECS.values()],
        "engine_paused": state is None or not state.trading_enabled,
        "running_version": state.config_version if state else None,
        "pending_restart": (state.details or {}).get("pending_restart", []) if state else [],
    }


@router.post("/validate")
async def validate(body: ConfigIn, principal: CurrentPrincipal, ctx: Ctx, db: DB
                   ) -> dict[str, Any]:
    active = await _active(db, ctx)
    _, errors, changes = await _evaluate(db, active, body.config)
    return _evaluation_json(errors, changes, await _engine_paused(db))


async def _save(*, db: AsyncSession, ctx: AppContext, request: Request, principal: Admin,
                data: dict[str, Any], base_version: int, comment: str, action: str
                ) -> dict[str, Any]:
    active = await _active(db, ctx)
    if active.version != base_version:
        raise Problem(409, "Settings changed meanwhile",
                      f"Version {active.version} is now active; reload and re-apply.",
                      extra={"active_version": active.version})
    proposed, errors, changes = await _evaluate(db, active, data)
    if errors or proposed is None:
        raise Problem(422, "Invalid settings", errors=_errors_json(errors))
    if not changes:
        raise Problem(422, "No changes", "The proposed settings equal the active version.")
    paused = await _engine_paused(db)
    blocked = [c.key for c in changes if c.requires_pause]
    if blocked and not paused:
        raise Problem(423, "Pause trading to apply these changes",
                      "Loosening a risk limit (or changing a pause-only setting) requires the "
                      "engine to be paused.",
                      errors=[{"key": k, "message": "requires trading to be paused"}
                              for k in blocked])
    try:
        new = await settings_repo.activate_new(db, config=proposed.to_json(),
                                               base_version=base_version,
                                               user_id=principal.user.id, comment=comment)
    except settings_repo.StaleSettingsVersion as exc:
        raise Problem(409, "Settings changed meanwhile",
                      extra={"active_version": exc.active_version}) from exc
    await users_repo.audit(
        db, action=action, user_id=principal.user.id, target=f"settings:v{new.version}",
        before={"version": active.version},
        after={"version": new.version, "comment": comment,
               "changes": [c.to_json() for c in changes]},
        ip=client_ip(request), user_agent=user_agent(request))
    await ctx.publish("settings:changed", {"version": new.version})
    return {**_version_json(new, principal.user.email),
            **_evaluation_json([], changes, paused)}


@router.put("", dependencies=[Depends(mutation_limit)])
async def save(body: SaveIn, principal: Admin, request: Request, ctx: Ctx, db: DB
               ) -> dict[str, Any]:
    return await _save(db=db, ctx=ctx, request=request, principal=principal, data=body.config,
                       base_version=body.base_version, comment=body.comment,
                       action="SETTINGS_UPDATE")


@router.get("/history")
async def history(principal: CurrentPrincipal, db: DB,
                  limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    rows = await settings_repo.history(db, limit)
    out = []
    for i, row in enumerate(rows[:limit]):
        prev = rows[i + 1] if i + 1 < len(rows) else None
        changes = (diff_configs(parse_config(prev.config), parse_config(row.config))
                   if prev is not None else [])
        out.append({**_version_json(row, await _author(db, row)),
                    "changes": [c.to_json() for c in changes]})
    return out


@router.post("/rollback", dependencies=[Depends(mutation_limit)])
async def rollback(body: RollbackIn, principal: Admin, request: Request, ctx: Ctx, db: DB
                   ) -> dict[str, Any]:
    target = await settings_repo.get_version(db, body.version)
    if target is None:
        raise Problem(404, "Settings version not found")
    comment = body.comment or f"Rollback to version {body.version}"
    return await _save(db=db, ctx=ctx, request=request, principal=principal,
                       data=target.config, base_version=body.base_version, comment=comment,
                       action="SETTINGS_ROLLBACK")


@router.get("/export")
async def export(principal: CurrentPrincipal, ctx: Ctx, db: DB) -> Response:
    active = await _active(db, ctx)
    text = (f"# Intraday App settings, version {active.version}\n"
            + yaml.safe_dump(active.config, sort_keys=False))
    return Response(text, media_type="application/yaml", headers={
        "Content-Disposition": f'attachment; filename="settings-v{active.version}.yaml"'})


@router.post("/import", dependencies=[Depends(mutation_limit)])
async def import_yaml(body: ImportIn, principal: Admin, request: Request, ctx: Ctx, db: DB
                      ) -> dict[str, Any]:
    try:
        data = yaml.safe_load(body.yaml)
    except yaml.YAMLError as exc:
        raise Problem(422, "Invalid YAML", str(exc)) from exc
    if not isinstance(data, dict):
        raise Problem(422, "Invalid YAML", "Top level must be a mapping of setting groups.")
    return await _save(db=db, ctx=ctx, request=request, principal=principal, data=data,
                       base_version=body.base_version,
                       comment=body.comment or "Imported from YAML", action="SETTINGS_IMPORT")
