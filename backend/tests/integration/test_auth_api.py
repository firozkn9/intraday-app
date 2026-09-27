"""Auth: login, cookies, lockout, CSRF, origin, rate limits, sessions (docs/design/05 §1-2)."""

from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, select, update

from intraday.api.context import AppContext
from intraday.api.security.tokens import CSRF_HEADER, SESSION_COOKIE
from intraday.core.timeutil import utcnow
from intraday.db.models import AuditLog, Session, User
from intraday.db.session import transaction
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, login

API = "/api/v1"


async def _audit_actions(ctx: AppContext) -> list[str]:
    async with ctx.sessionmaker() as s:
        return list((await s.execute(select(AuditLog.action).order_by(AuditLog.ts))).scalars())


async def test_login_sets_hardened_cookies(client: httpx.AsyncClient, admin: dict[str, Any],
                                           ctx: AppContext) -> None:
    resp = await login(client)
    assert resp.status_code == 200
    assert resp.json()["email"] == ADMIN_EMAIL
    cookies = resp.headers.get_list("set-cookie")
    sid = next(c for c in cookies if c.startswith("sid="))
    csrf = next(c for c in cookies if c.startswith("csrf="))
    assert "HttpOnly" in sid and "SameSite=strict" in sid and "Path=/" in sid
    assert "HttpOnly" not in csrf
    async with ctx.sessionmaker() as s:
        row = (await s.execute(select(Session))).scalar_one()
    # Only an HMAC digest of the token is stored.
    assert row.id_hash != client.cookies[SESSION_COOKIE] and len(row.id_hash) == 64
    assert await _audit_actions(ctx) == ["LOGIN"]


async def test_email_is_case_insensitive(client: httpx.AsyncClient, admin: dict[str, Any]
                                         ) -> None:
    assert (await login(client, email=ADMIN_EMAIL.upper())).status_code == 200


async def test_wrong_password_is_generic_401_and_audited(
        client: httpx.AsyncClient, admin: dict[str, Any], ctx: AppContext) -> None:
    bad = await login(client, password="wrong-password-123")
    unknown = await login(client, email="nobody@example.com", password="wrong-password-123")
    assert bad.status_code == unknown.status_code == 401
    assert bad.json()["title"] == unknown.json()["title"] == "Invalid email or password"
    assert bad.headers["content-type"].startswith("application/problem+json")
    assert await _audit_actions(ctx) == ["LOGIN_FAILED", "LOGIN_FAILED"]


async def test_lockout_after_ten_failures(client: httpx.AsyncClient, admin: dict[str, Any],
                                          ctx: AppContext) -> None:
    for i in range(10):
        ctx.limiter.reset()  # type: ignore[attr-defined] - isolate lockout from rate limit
        resp = await login(client, password=f"wrong-password-{i}")
        assert resp.status_code == 401
    ctx.limiter.reset()  # type: ignore[attr-defined]
    locked = await login(client)  # correct password, but locked
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 800
    async with ctx.sessionmaker() as s:
        user = (await s.execute(select(User))).scalar_one()
    assert user.locked_until is not None
    assert "LOGIN_LOCKED" in await _audit_actions(ctx)


async def test_login_rate_limited_per_ip(client: httpx.AsyncClient, admin: dict[str, Any]
                                         ) -> None:
    codes = [(await login(client, password="wrong-password-x")).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429


async def test_cross_origin_login_refused(client: httpx.AsyncClient, admin: dict[str, Any]
                                          ) -> None:
    resp = await client.post(f"{API}/auth/login",
                             json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                             headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


async def test_endpoints_require_auth(client: httpx.AsyncClient) -> None:
    for path in ("/auth/me", "/status", "/settings", "/commands", "/openapi.json"):
        assert (await client.get(f"{API}{path}")).status_code == 401, path


async def test_health_is_public(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"{API}/health")).json() == {"status": "ok"}
    ready = await client.get(f"{API}/health/ready")
    assert ready.status_code == 200 and ready.json()["checks"]["database"] == "ok"


async def test_unsafe_requests_need_csrf_token(auth_client: httpx.AsyncClient) -> None:
    token = auth_client.headers.pop(CSRF_HEADER)
    assert (await auth_client.post(f"{API}/controls/pause")).status_code == 403
    wrong = await auth_client.post(f"{API}/controls/pause", headers={CSRF_HEADER: "nope"})
    assert wrong.status_code == 403
    ok = await auth_client.post(f"{API}/controls/pause", headers={CSRF_HEADER: token})
    assert ok.status_code == 202


async def test_unsafe_requests_refuse_foreign_origin(auth_client: httpx.AsyncClient) -> None:
    resp = await auth_client.post(f"{API}/controls/pause",
                                  headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


async def test_logout_revokes_session(auth_client: httpx.AsyncClient, ctx: AppContext) -> None:
    sid = auth_client.cookies[SESSION_COOKIE]
    assert (await auth_client.post(f"{API}/auth/logout")).status_code == 204
    auth_client.cookies.set(SESSION_COOKIE, sid)  # replaying the old cookie must fail
    assert (await auth_client.get(f"{API}/auth/me")).status_code == 401


async def test_idle_session_expires(auth_client: httpx.AsyncClient, ctx: AppContext) -> None:
    async with transaction(ctx.sessionmaker) as s:
        await s.execute(update(Session).values(last_seen_at=utcnow() - timedelta(hours=13)))
    assert (await auth_client.get(f"{API}/auth/me")).status_code == 401


async def test_absolute_session_expiry(auth_client: httpx.AsyncClient, ctx: AppContext) -> None:
    async with transaction(ctx.sessionmaker) as s:
        await s.execute(update(Session).values(created_at=utcnow() - timedelta(days=8)))
    assert (await auth_client.get(f"{API}/auth/me")).status_code == 401


async def test_csrf_reissue(auth_client: httpx.AsyncClient) -> None:
    old = auth_client.headers[CSRF_HEADER]
    assert (await auth_client.get(f"{API}/auth/csrf")).status_code == 204
    new = auth_client.cookies["csrf"]
    assert new != old
    stale = await auth_client.post(f"{API}/controls/pause", headers={CSRF_HEADER: old})
    assert stale.status_code == 403
    auth_client.headers[CSRF_HEADER] = new
    assert (await auth_client.post(f"{API}/controls/pause")).status_code == 202


async def test_change_password(auth_client: httpx.AsyncClient, ctx: AppContext) -> None:
    other = httpx.AsyncClient(transport=auth_client._transport, base_url=str(auth_client.base_url),
                              headers={"Origin": "http://testserver"})
    assert (await login(other)).status_code == 200

    weak = await auth_client.post(f"{API}/auth/change-password",
                                  json={"current_password": ADMIN_PASSWORD, "new_password": "x"})
    assert weak.status_code == 422
    wrong = await auth_client.post(f"{API}/auth/change-password",
                                   json={"current_password": "nope-nope-nope",
                                         "new_password": "another-good-password"})
    assert wrong.status_code == 403
    ok = await auth_client.post(f"{API}/auth/change-password",
                                json={"current_password": ADMIN_PASSWORD,
                                      "new_password": "another-good-password"})
    assert ok.status_code == 204
    # Current session survives; other sessions are revoked.
    assert (await auth_client.get(f"{API}/auth/me")).status_code == 200
    assert (await other.get(f"{API}/auth/me")).status_code == 401
    await other.aclose()
    assert (await login(auth_client, password="another-good-password")).status_code == 200
    async with ctx.sessionmaker() as s:
        n = (await s.execute(select(func.count()).select_from(AuditLog)
                             .where(AuditLog.action == "PASSWORD_CHANGED"))).scalar_one()
    assert n == 1
