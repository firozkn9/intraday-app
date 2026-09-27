"""/auth: login, logout, CSRF token, current user, password change."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from intraday.api.context import AppContext
from intraday.api.deps import (
    DB,
    Admin,
    Ctx,
    CurrentPrincipal,
    check_origin,
    client_ip,
    enforce_limit,
    mutation_limit,
    user_agent,
)
from intraday.api.errors import Problem, problem_response
from intraday.api.security import passwords
from intraday.api.security.rate_limit import LOGIN_PER_ACCOUNT, LOGIN_PER_IP
from intraday.api.security.tokens import (
    CSRF_COOKIE,
    SESSION_ABSOLUTE_TIMEOUT,
    SESSION_COOKIE,
    new_token,
)
from intraday.core.timeutil import utcnow
from intraday.db.models import User
from intraday.db.repositories import users as users_repo

router = APIRouter(prefix="/auth", tags=["auth"])

MAX_FAILED_LOGINS = 10
LOCKOUT = timedelta(minutes=15)
GENERIC_LOGIN_ERROR = "Invalid email or password"


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=passwords.MAX_PASSWORD_LENGTH)


class UserOut(BaseModel):
    id: str
    email: str
    role: str


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=passwords.MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=passwords.MAX_PASSWORD_LENGTH)


def _user_out(user: User) -> UserOut:
    return UserOut(id=str(user.id), email=user.email, role=user.role)


def _set_cookies(response: Response, ctx: AppContext, session_token: str,
                 csrf_token: str) -> None:
    max_age = int(SESSION_ABSOLUTE_TIMEOUT.total_seconds())
    common = {"secure": ctx.env.cookie_secure, "samesite": "strict", "path": "/",
              "max_age": max_age}
    response.set_cookie(SESSION_COOKIE, session_token, httponly=True, **common)  # type: ignore[arg-type]
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, **common)  # type: ignore[arg-type]


def _clear_cookies(response: Response, ctx: AppContext) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=ctx.env.cookie_secure, samesite="strict")


@router.post("/login", response_model=UserOut)
async def login(body: LoginIn, request: Request, ctx: Ctx, db: DB) -> Response:
    check_origin(request, ctx)
    ip, ua, email = client_ip(request), user_agent(request), body.email.strip().lower()
    await enforce_limit(ctx, LOGIN_PER_IP, ip)
    await enforce_limit(ctx, LOGIN_PER_ACCOUNT, email)

    now = utcnow()
    user = await users_repo.get_user_by_email(db, email)
    if user is not None and user.locked_until is not None and user.locked_until > now:
        await users_repo.audit(db, action="LOGIN_LOCKED", user_id=user.id, ip=ip,
                               user_agent=ua, result="DENIED")
        retry = int((user.locked_until - now).total_seconds()) + 1
        # Return (not raise) so the audit row commits.
        return problem_response(request, 429, "Too many failed attempts; try again later",
                                headers={"Retry-After": str(retry)})

    ok = passwords.verify_password(user.password_hash if user else None, body.password)
    if not ok or user is None or not user.is_active:
        if user is not None:
            user.failed_login_count += 1
            if user.failed_login_count >= MAX_FAILED_LOGINS:
                user.locked_until = now + LOCKOUT
                user.failed_login_count = 0
        await users_repo.audit(db, action="LOGIN_FAILED", user_id=user.id if user else None,
                               target=email, ip=ip, user_agent=ua, result="DENIED")
        return problem_response(request, 401, GENERIC_LOGIN_ERROR)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(body.password)

    session_token, csrf_token = new_token(), new_token()
    await users_repo.create_session(
        db, id_hash=ctx.tokens.digest(session_token), user_id=user.id,
        csrf_token_hash=ctx.tokens.digest(csrf_token), now=now,
        expires_at=now + SESSION_ABSOLUTE_TIMEOUT, ip=ip, user_agent=ua)
    await users_repo.audit(db, action="LOGIN", user_id=user.id, ip=ip, user_agent=ua)

    response = JSONResponse(_user_out(user).model_dump())
    _set_cookies(response, ctx, session_token, csrf_token)
    return response


@router.post("/logout", status_code=204, dependencies=[Depends(mutation_limit)])
async def logout(principal: CurrentPrincipal, request: Request, ctx: Ctx, db: DB) -> Response:
    await users_repo.revoke_session(db, principal.session.id_hash, utcnow())
    await users_repo.audit(db, action="LOGOUT", user_id=principal.user.id,
                           ip=client_ip(request), user_agent=user_agent(request))
    response = Response(status_code=204)
    _clear_cookies(response, ctx)
    return response


@router.get("/csrf", status_code=204)
async def csrf(principal: CurrentPrincipal, request: Request, ctx: Ctx) -> Response:
    """Re-issue the CSRF cookie for the current session (e.g. after it was cleared)."""
    session_token = request.cookies[SESSION_COOKIE]
    csrf_token = new_token()
    principal.session.csrf_token_hash = ctx.tokens.digest(csrf_token)
    response = Response(status_code=204)
    _set_cookies(response, ctx, session_token, csrf_token)
    return response


@router.get("/me", response_model=UserOut)
async def me(principal: CurrentPrincipal) -> UserOut:
    return _user_out(principal.user)


@router.post("/change-password", status_code=204, dependencies=[Depends(mutation_limit)])
async def change_password(body: ChangePasswordIn, principal: Admin, request: Request,
                          db: DB) -> Response:
    user = principal.user
    if not passwords.verify_password(user.password_hash, body.current_password):
        raise Problem(403, "Current password is incorrect")
    try:
        passwords.check_policy(body.new_password, email=user.email)
    except passwords.WeakPassword as exc:
        raise Problem(422, "Weak password", str(exc),
                      errors=[{"key": "new_password", "message": str(exc)}]) from exc
    now = utcnow()
    user.password_hash = passwords.hash_password(body.new_password)
    user.password_changed_at = now
    await users_repo.revoke_other_sessions(db, user.id, principal.session.id_hash, now)
    await users_repo.audit(db, action="PASSWORD_CHANGED", user_id=user.id,
                           ip=client_ip(request), user_agent=user_agent(request))
    return Response(status_code=204)
