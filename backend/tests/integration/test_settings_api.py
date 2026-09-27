"""Settings API: versioning, validation, pause rule, history, rollback, import/export."""

from typing import Any

import httpx
import yaml
from sqlalchemy import select

from intraday.api.context import AppContext
from intraday.db.models import AuditLog, SettingsVersion
from intraday.db.repositories import engine as engine_repo
from intraday.db.session import transaction

API = "/api/v1/settings"


async def _get(client: httpx.AsyncClient) -> dict[str, Any]:
    resp = await client.get(API)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _set(config: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    node = config
    *path, leaf = key.split(".")
    for p in path:
        node = node[p]
    node[leaf] = value
    return config


async def _set_trading(ctx: AppContext, enabled: bool) -> None:
    async with transaction(ctx.sessionmaker) as s:
        await engine_repo.upsert_state(s, status="RUNNING" if enabled else "PAUSED",
                                       trading_enabled=enabled)


async def _put(client: httpx.AsyncClient, base: int, config: dict[str, Any],
               comment: str = "test") -> httpx.Response:
    return await client.put(API, json={"base_version": base, "config": config, "comment": comment})


async def test_initial_version_from_defaults(auth_client: httpx.AsyncClient) -> None:
    body = await _get(auth_client)
    assert body["version"] == 1 and body["is_active"] is True
    assert body["config"]["risk"]["max_daily_loss"] == "500"
    assert body["comment"].startswith("Initial defaults")
    fields = {f["key"]: f for f in body["fields"]}
    assert fields["risk.max_daily_loss"]["policy"] == "PAUSED"
    assert fields["risk.max_daily_loss"]["loosen"] == "higher"
    assert fields["data.bar_interval"]["policy"] == "LOCKED"
    assert fields["ai.mode"]["options"] == ["OFF", "EXPLAIN", "VETO"]
    assert body["engine_paused"] is True


async def test_validate_reports_diff_and_policy(auth_client: httpx.AsyncClient,
                                                ctx: AppContext) -> None:
    await _set_trading(ctx, True)
    cfg = (await _get(auth_client))["config"]
    _set(cfg, "risk.max_daily_loss", "400")        # tightening
    _set(cfg, "risk.max_open_positions", 6)        # loosening
    _set(cfg, "data.feed", "sip")                  # restart
    resp = await auth_client.post(f"{API}/validate", json={"config": cfg})
    body = resp.json()
    assert body["valid"] is True
    changes = {c["key"]: c for c in body["changes"]}
    assert changes["risk.max_daily_loss"]["loosening"] is False
    assert changes["risk.max_open_positions"]["requires_pause"] is True
    assert body["requires_pause"] and body["blocked_by_running"]
    assert body["pending_restart"] == ["data.feed"]


async def test_validate_reports_field_errors(auth_client: httpx.AsyncClient) -> None:
    cfg = (await _get(auth_client))["config"]
    _set(cfg, "risk.max_open_positions", 50)
    body = (await auth_client.post(f"{API}/validate", json={"config": cfg})).json()
    assert body["valid"] is False
    assert body["errors"][0]["key"] == "risk.max_open_positions"


async def test_tightening_allowed_while_running(auth_client: httpx.AsyncClient,
                                                ctx: AppContext, fake_redis: Any) -> None:
    await _set_trading(ctx, True)
    pubsub = fake_redis.pubsub()
    await pubsub.subscribe("settings:changed")
    cfg = _set((await _get(auth_client))["config"], "risk.max_daily_loss", "400")
    resp = await _put(auth_client, 1, cfg, "tighten")
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    body = await _get(auth_client)
    assert body["version"] == 2 and body["config"]["risk"]["max_daily_loss"] == "400"
    msg = None
    for _ in range(3):  # the first read may return the subscribe confirmation
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        if msg is not None:
            break
    assert msg is not None and b'"version": 2' in msg["data"]


async def test_loosening_refused_while_running(auth_client: httpx.AsyncClient,
                                               ctx: AppContext) -> None:
    await _set_trading(ctx, True)
    cfg = _set((await _get(auth_client))["config"], "risk.max_daily_loss", "600")
    resp = await _put(auth_client, 1, cfg)
    assert resp.status_code == 423
    assert resp.json()["errors"][0]["key"] == "risk.max_daily_loss"
    assert (await _get(auth_client))["version"] == 1


async def test_loosening_allowed_while_paused(auth_client: httpx.AsyncClient,
                                              ctx: AppContext) -> None:
    await _set_trading(ctx, False)
    cfg = _set((await _get(auth_client))["config"], "risk.max_daily_loss", "600")
    assert (await _put(auth_client, 1, cfg)).status_code == 200


async def test_ai_mode_change_requires_pause(auth_client: httpx.AsyncClient,
                                             ctx: AppContext) -> None:
    await _set_trading(ctx, True)
    cfg = (await _get(auth_client))["config"]
    _set(cfg, "ai.mode", "EXPLAIN")
    _set(cfg, "ai.model", "some-model")
    assert (await _put(auth_client, 1, cfg)).status_code == 423


async def test_stale_base_version_conflicts(auth_client: httpx.AsyncClient) -> None:
    cfg = _set((await _get(auth_client))["config"], "risk.max_daily_loss", "400")
    assert (await _put(auth_client, 1, cfg)).status_code == 200
    cfg2 = _set((await _get(auth_client))["config"], "risk.max_daily_loss", "300")
    resp = await _put(auth_client, 1, cfg2)
    assert resp.status_code == 409 and resp.json()["active_version"] == 2


async def test_invalid_locked_and_hard_limit_rejected(auth_client: httpx.AsyncClient) -> None:
    base = (await _get(auth_client))["config"]
    bad_bounds = _set(dict(base), "risk.max_open_positions", 0)
    assert (await _put(auth_client, 1, bad_bounds)).status_code == 422

    base = (await _get(auth_client))["config"]
    locked = _set(base, "risk.allow_short_selling", True)
    assert (await _put(auth_client, 1, locked)).status_code == 422

    base = (await _get(auth_client))["config"]
    over_hard_limit = _set(base, "risk.max_loss_per_trade", "450")  # within 1% of 100k
    _set(over_hard_limit, "risk.max_daily_loss", "3500")             # > 3% of 100k
    resp = await _put(auth_client, 1, over_hard_limit)
    assert resp.status_code == 422
    assert "risk.max_daily_loss" in [e["key"] for e in resp.json()["errors"]]


async def test_raising_paper_balance_cannot_lift_hard_limits(
        auth_client: httpx.AsyncClient) -> None:
    cfg = (await _get(auth_client))["config"]
    _set(cfg, "risk.paper_initial_balance", "1000000")
    _set(cfg, "risk.max_daily_loss", "5000")       # fine at 1M equity, not at 100k
    _set(cfg, "risk.max_loss_per_trade", "100")
    resp = await _put(auth_client, 1, cfg)
    assert resp.status_code == 422


async def test_no_op_save_rejected(auth_client: httpx.AsyncClient) -> None:
    cfg = (await _get(auth_client))["config"]
    assert (await _put(auth_client, 1, cfg)).status_code == 422


async def test_history_and_rollback(auth_client: httpx.AsyncClient, ctx: AppContext) -> None:
    cfg = _set((await _get(auth_client))["config"], "risk.max_daily_loss", "400")
    assert (await _put(auth_client, 1, cfg, "v2")).status_code == 200
    hist = (await auth_client.get(f"{API}/history")).json()
    assert [h["version"] for h in hist] == [2, 1]
    assert hist[0]["changes"][0]["key"] == "risk.max_daily_loss"
    assert hist[0]["created_by"] == "admin@example.com"

    resp = await auth_client.post(f"{API}/rollback", json={"version": 1, "base_version": 2})
    assert resp.status_code == 200
    body = await _get(auth_client)
    assert body["version"] == 3 and body["config"]["risk"]["max_daily_loss"] == "500"
    assert body["comment"] == "Rollback to version 1"
    async with ctx.sessionmaker() as s:
        actions = list((await s.execute(select(AuditLog.action).order_by(AuditLog.ts))).scalars())
        active = (await s.execute(select(SettingsVersion).where(SettingsVersion.is_active))
                  ).scalars().all()
    assert actions[-2:] == ["SETTINGS_UPDATE", "SETTINGS_ROLLBACK"]
    assert len(active) == 1


async def test_rollback_of_loosening_needs_pause(auth_client: httpx.AsyncClient,
                                                 ctx: AppContext) -> None:
    cfg = _set((await _get(auth_client))["config"], "risk.max_daily_loss", "400")
    assert (await _put(auth_client, 1, cfg)).status_code == 200
    await _set_trading(ctx, True)
    resp = await auth_client.post(f"{API}/rollback", json={"version": 1, "base_version": 2})
    assert resp.status_code == 423


async def test_export_import_round_trip(auth_client: httpx.AsyncClient) -> None:
    resp = await auth_client.get(f"{API}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/yaml")
    data = yaml.safe_load(resp.text)
    data["risk"]["max_daily_loss"] = "450"
    resp = await auth_client.post(f"{API}/import", json={
        "yaml": yaml.safe_dump(data), "base_version": 1})
    assert resp.status_code == 200, resp.text
    assert (await _get(auth_client))["config"]["risk"]["max_daily_loss"] == "450"


async def test_import_rejects_garbage(auth_client: httpx.AsyncClient) -> None:
    for text in ("- just\n- a list\n", "key: [unclosed"):
        resp = await auth_client.post(f"{API}/import", json={"yaml": text, "base_version": 1})
        assert resp.status_code == 422
