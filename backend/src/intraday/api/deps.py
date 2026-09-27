"""FastAPI dependencies: DB session, auth, CSRF, origin check, rate limits."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from intraday.api.context import AppContext
from intraday.api.errors import Problem
from intraday.api.security.rate_limit import MUTATIONS, READS, Limit
from intraday.api.security.tokens import (
    CSRF_HEADER,
    SESSION_ABSOLUTE_TIMEOUT,
    SESSION_COOKIE,
    SESSION_IDLE_TIMEOUT,
)
from intraday.core.timeutil import utcnow
from intraday.db.models import Session as SessionRow
from intraday.db.models import User
from intraday.db.repositories import users as users_repo

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def get_db(ctx: Ctx) -> AsyncIterator[AsyncSession]:
    """One transaction per request: commits on success, rolls back on exceptions."""
    async with ctx.sessionmaker() as session, session.begin():
        yield session


DB = Annotated[AsyncSession, Depends(get_db)]


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def check_origin(request: Request, ctx: AppContext) -> None:
    """Reject cross-origin unsafe requests. Browsers always send Origin on POST."""
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != ctx.env.public_origin.rstrip("/"):
        raise Problem(403, "Cross-origin request refused")


@dataclass(frozen=True)
class Principal:
    user: User
    session: SessionRow


async def _load_principal(request: Request, ctx: AppContext, db: AsyncSession) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise Problem(401, "Not authenticated")
    row = await users_repo.get_session(db, ctx.tokens.digest(token))
    now = utcnow()
    if (row is None or row.revoked_at is not None or row.expires_at <= now
            or row.last_seen_at + SESSION_IDLE_TIMEOUT <= now
            or row.created_at + SESSION_ABSOLUTE_TIMEOUT <= now):
        raise Problem(401, "Session expired")
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise Problem(401, "Not authenticated")
    await users_repo.touch_session(db, row.id_hash, now)
    return Principal(user=user, session=row)


async def current_principal(request: Request, ctx: Ctx, db: DB) -> Principal:
    principal = await _load_principal(request, ctx, db)
    if request.method in UNSAFE_METHODS:
        check_origin(request, ctx)
        if not ctx.tokens.matches(request.headers.get(CSRF_HEADER),
                                  principal.session.csrf_token_hash):
            raise Problem(403, "CSRF token missing or invalid")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


async def require_admin(principal: CurrentPrincipal) -> Principal:
    if principal.user.role != "admin":
        raise Problem(403, "Admin role required")
    return principal


Admin = Annotated[Principal, Depends(require_admin)]


async def enforce_limit(ctx: AppContext, limit: Limit, key: str) -> None:
    decision = await ctx.limiter.hit(limit, key)
    if not decision.allowed:
        raise Problem(429, "Too many requests", headers={"Retry-After": str(decision.retry_after)})


async def read_limit(request: Request, ctx: Ctx) -> None:
    await enforce_limit(ctx, READS, client_ip(request))


async def mutation_limit(request: Request, ctx: Ctx) -> None:
    await enforce_limit(ctx, MUTATIONS, client_ip(request))
