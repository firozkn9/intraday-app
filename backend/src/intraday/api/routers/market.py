"""/watchlist and /market (docs/design/05 §6-7). The API holds no Alpaca keys:
it validates hand-entered data and cached Alpaca asset data; the engine syncs assets."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
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
from intraday.core.assets import manual_rules
from intraday.core.runtime_config import parse_config
from intraday.core.timeutil import ET, utcnow
from intraday.db.models import Order, Position, Symbol
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import marketdata as md_repo
from intraday.db.repositories import settings as settings_repo
from intraday.db.repositories import users as users_repo
from intraday.marketdata.watchlist import eligibility_of, meta_of, stream_plan

router = APIRouter(tags=["market"], dependencies=[Depends(read_limit)])

TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
MAX_BAR_QUERY_DAYS = 10
OPEN_ORDER_STATUSES = ("PENDING_SUBMIT", "SUBMITTED", "UNKNOWN", "PARTIALLY_FILLED",
                       "PENDING_CANCEL")


class SymbolIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=8)
    role: Literal["TRADABLE", "CONTEXT"]
    asset_type: Literal["COMMON_STOCK", "ETF", "OTHER"]
    country: str | None = Field(default=None, min_length=2, max_length=2)
    name: str | None = Field(default=None, max_length=256)
    sector: str | None = Field(default=None, max_length=64)
    sector_etf: str | None = Field(default=None, max_length=8)
    market_cap: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)
    enabled: bool = True

    @field_validator("symbol", "sector_etf")
    @classmethod
    def _ticker(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if not TICKER.match(v):
            raise ValueError("not a valid ticker")
        return v

    @field_validator("country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        return v.strip().upper() if v else None


class SymbolPatch(BaseModel):
    role: Literal["TRADABLE", "CONTEXT"] | None = None
    asset_type: Literal["COMMON_STOCK", "ETF", "OTHER"] | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    name: str | None = Field(default=None, max_length=256)
    sector: str | None = Field(default=None, max_length=64)
    sector_etf: str | None = Field(default=None, max_length=8)
    market_cap: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


class BackfillIn(BaseModel):
    days: int = Field(default=30, ge=1, le=60)
    full: bool = False


def _symbol_json(row: Symbol, now: datetime) -> dict[str, Any]:
    return {
        "symbol": row.symbol, "role": row.role, "name": row.name, "asset_type": row.asset_type,
        "country": row.country, "sector": row.sector, "sector_etf": row.sector_etf,
        "market_cap": int(row.market_cap) if row.market_cap is not None else None,
        "enabled": row.enabled, "notes": row.notes,
        "alpaca": {"class": row.alpaca_class, "exchange": row.alpaca_exchange,
                   "name": row.alpaca_name, "status": row.alpaca_status,
                   "tradable": row.alpaca_tradable,
                   "synced_at": row.alpaca_synced_at.isoformat() if row.alpaca_synced_at else None},
        "eligibility": eligibility_of(row, now).to_json() if row.role == "TRADABLE" else None,
    }


async def _config(db: AsyncSession, ctx: AppContext) -> Any:
    active = await settings_repo.get_active(db)
    return parse_config(active.config) if active else ctx.defaults


async def _watchlist_body(db: AsyncSession, ctx: AppContext) -> dict[str, Any]:
    rows = await md_repo.list_symbols(db)
    now = utcnow()
    cfg = await _config(db, ctx)
    return {
        "symbols": [_symbol_json(r, now) for r in rows],
        "stream_budget": stream_plan(rows, ctx.env.alpaca_stream_symbol_limit).to_json(),
        "max_watchlist_size": cfg.universe.max_watchlist_size,
    }


def _validate_tradable(meta_row: Any) -> None:
    failed = {k: r.detail for k, r in manual_rules(meta_of(meta_row)).items() if not r.passed}
    if failed:
        raise Problem(422, "Not a tradable U.S. common stock",
                      "CLAUDE.md: only U.S.-issuer common stock on NYSE/Nasdaq can be tradable.",
                      errors=[{"key": k, "message": v} for k, v in failed.items()])


async def _check_capacity(db: AsyncSession, ctx: AppContext, *, adding_tradable: bool) -> None:
    if not adding_tradable:
        return
    cfg = await _config(db, ctx)
    n = (await db.execute(select(func.count()).select_from(Symbol).where(
        Symbol.role == "TRADABLE", Symbol.enabled))).scalar_one()
    if n >= cfg.universe.max_watchlist_size:
        raise Problem(422, "Watchlist is full",
                      f"universe.max_watchlist_size is {cfg.universe.max_watchlist_size}.")


async def _warnings(db: AsyncSession, ctx: AppContext) -> list[str]:
    plan = stream_plan(await md_repo.list_symbols(db), ctx.env.alpaca_stream_symbol_limit)
    if plan.dropped:
        return [f"Stream budget ({plan.limit} symbols) exceeded; not streamed: "
                f"{', '.join(plan.dropped)}"]
    return []


async def _after_change(request: Request, ctx: AppContext, db: AsyncSession, principal: Admin,
                        action: str, symbol: str, before: dict[str, Any] | None,
                        after: dict[str, Any] | None) -> None:
    await users_repo.audit(db, action=action, user_id=principal.user.id,
                           target=f"symbol:{symbol}", before=before, after=after,
                           ip=client_ip(request), user_agent=user_agent(request))
    await ctx.publish("watchlist:changed", {"symbol": symbol})


def _editable(row: Symbol) -> dict[str, Any]:
    return {k: getattr(row, k) for k in ("role", "asset_type", "country", "name", "sector",
                                         "sector_etf", "notes", "enabled")} | {
        "market_cap": int(row.market_cap) if row.market_cap is not None else None}


@router.get("/watchlist")
async def list_watchlist(principal: CurrentPrincipal, ctx: Ctx, db: DB) -> dict[str, Any]:
    return await _watchlist_body(db, ctx)


@router.post("/watchlist", status_code=201, dependencies=[Depends(mutation_limit)])
async def add_symbol(body: SymbolIn, principal: Admin, request: Request, ctx: Ctx, db: DB
                     ) -> dict[str, Any]:
    if await db.get(Symbol, body.symbol) is not None:
        raise Problem(409, "Symbol already on the watchlist")
    row = Symbol(**body.model_dump(exclude={"market_cap"}),
                 market_cap=Decimal(body.market_cap) if body.market_cap is not None else None)
    if row.role == "TRADABLE":
        _validate_tradable(row)
        await _check_capacity(db, ctx, adding_tradable=row.enabled)
    db.add(row)
    await db.flush()
    await _after_change(request, ctx, db, principal, "WATCHLIST_ADD", row.symbol, None,
                        _editable(row))
    return {"symbol": _symbol_json(row, utcnow()), "warnings": await _warnings(db, ctx)}


@router.patch("/watchlist/{symbol}", dependencies=[Depends(mutation_limit)])
async def update_symbol(symbol: str, body: SymbolPatch, principal: Admin, request: Request,
                        ctx: Ctx, db: DB) -> dict[str, Any]:
    row = await db.get(Symbol, symbol.upper())
    if row is None:
        raise Problem(404, "Symbol not on the watchlist")
    before = _editable(row)
    changes = body.model_dump(exclude_unset=True)
    becomes_active_tradable = (changes.get("role", row.role) == "TRADABLE"
                               and changes.get("enabled", row.enabled)
                               and not (row.role == "TRADABLE" and row.enabled))
    for key, value in changes.items():
        if key == "market_cap":
            value = Decimal(value) if value is not None else None
        elif key in ("country", "sector_etf") and value:
            value = value.upper()
        setattr(row, key, value)
    if row.role == "TRADABLE":
        _validate_tradable(row)
        await _check_capacity(db, ctx, adding_tradable=becomes_active_tradable)
    row.updated_at = utcnow()
    await db.flush()
    await _after_change(request, ctx, db, principal, "WATCHLIST_UPDATE", row.symbol, before,
                        _editable(row))
    return {"symbol": _symbol_json(row, utcnow()), "warnings": await _warnings(db, ctx)}


@router.delete("/watchlist/{symbol}", status_code=204, dependencies=[Depends(mutation_limit)])
async def delete_symbol(symbol: str, principal: Admin, request: Request, ctx: Ctx, db: DB
                        ) -> Response:
    row = await db.get(Symbol, symbol.upper())
    if row is None:
        raise Problem(404, "Symbol not on the watchlist")
    open_pos = (await db.execute(select(func.count()).select_from(Position).where(
        Position.symbol == row.symbol, Position.status.in_(("PENDING_ENTRY", "OPEN",
                                                            "EXITING"))))).scalar_one()
    open_orders = (await db.execute(select(func.count()).select_from(Order).where(
        Order.symbol == row.symbol, Order.status.in_(OPEN_ORDER_STATUSES)))).scalar_one()
    if open_pos or open_orders:
        raise Problem(409, "Symbol has an open position or order",
                      "Close it first; disabling the symbol is also possible.")
    before = _editable(row)
    await db.delete(row)
    await _after_change(request, ctx, db, principal, "WATCHLIST_DELETE", symbol.upper(), before,
                        None)
    return Response(status_code=204)


@router.get("/market/calendar")
async def calendar(principal: CurrentPrincipal, db: DB, start: date | None = None,
                   end: date | None = None) -> dict[str, Any]:
    today = utcnow().astimezone(ET).date()
    start = start or today - timedelta(days=7)
    end = end or today + timedelta(days=30)
    if (end - start).days > 400:
        raise Problem(422, "Range too large", "At most 400 days.")
    cal = await md_repo.load_calendar(db, start, end)
    return {"sessions": [{"date": s.session_date.isoformat(), "open_at": s.open_at.isoformat(),
                          "close_at": s.close_at.isoformat(), "early_close": s.early_close}
                         for s in cal.sessions_between(start, end)],
            "status": cal.status(utcnow())}


@router.get("/market/bars")
async def bars(principal: CurrentPrincipal, ctx: Ctx, db: DB,
               symbol: str = Query(min_length=1, max_length=8),
               start: datetime | None = None, end: datetime | None = None,
               feed: Literal["iex", "sip"] | None = None) -> dict[str, Any]:
    now = utcnow()
    end = end or now
    start = start or end - timedelta(days=1)
    if start.tzinfo is None or end.tzinfo is None:
        raise Problem(422, "Timestamps must include a timezone")
    if end - start > timedelta(days=MAX_BAR_QUERY_DAYS):
        raise Problem(422, "Range too large", f"At most {MAX_BAR_QUERY_DAYS} days.")
    feed = feed or (await _config(db, ctx)).data.feed
    rows = await md_repo.get_bars(db, symbol.upper(), feed, start, end)
    return {"symbol": symbol.upper(), "feed": feed,
            "bars": [{"t": r.ts.isoformat(), "o": str(r.open), "h": str(r.high),
                      "l": str(r.low), "c": str(r.close), "v": r.volume,
                      "vw": str(r.vwap) if r.vwap is not None else None} for r in rows]}


@router.post("/market/backfill", status_code=202, dependencies=[Depends(mutation_limit)])
async def backfill(body: BackfillIn, principal: Admin, request: Request, ctx: Ctx, db: DB
                   ) -> dict[str, Any]:
    cmd = await engine_repo.enqueue_command(db, command="BACKFILL", user_id=principal.user.id,
                                            params=body.model_dump())
    await users_repo.audit(db, action="COMMAND_BACKFILL", user_id=principal.user.id,
                           target=f"command:{cmd.id}", after=body.model_dump(),
                           ip=client_ip(request), user_agent=user_agent(request))
    await ctx.publish("commands:new", {"id": str(cmd.id)})
    return {"command_id": str(cmd.id), "status": cmd.status}
