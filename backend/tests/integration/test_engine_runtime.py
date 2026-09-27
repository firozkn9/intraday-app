"""Engine runtime (Phase 1): paper guard, gate, commands, settings, heartbeat, restarts."""

import asyncio
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.env import EnvSettings
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import EngineCommand, SettingsVersion, SystemEvent
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import settings as settings_repo
from intraday.db.session import transaction
from intraday.engine.runtime import EngineRuntime

API = "/api/v1"


def make_runtime(env: EnvSettings, sm: async_sessionmaker[AsyncSession], redis: Any = None,
                 **overrides: Any) -> EngineRuntime:
    if overrides:
        env = env.model_copy(update=overrides)
    return EngineRuntime(env, sm, redis, TradingConfig(), heartbeat_interval=0.05,
                         command_poll_interval=0.05, settings_poll_interval=0.05)


async def state(sm: async_sessionmaker[AsyncSession]) -> Any:
    async with sm() as s:
        return await engine_repo.get_state(s)


async def test_boot_paused_without_broker(env: EnvSettings, sessionmaker: Any) -> None:
    rt = make_runtime(env, sessionmaker)
    await rt.boot()
    st = await state(sessionmaker)
    # Without keys there is no broker (NO_BROKER) and no market data (DATA_STALE); with
    # no calendar, OUTSIDE_ENTRY_WINDOW. The top-priority status is BROKER ERROR.
    assert st.status == "BROKER ERROR" and st.trading_enabled is False
    reasons = [b["reason"] for b in st.blocks]
    assert reasons == ["NO_BROKER", "DATA_STALE", "PAUSED_BY_USER", "OUTSIDE_ENTRY_WINDOW"]
    assert st.details["broker"] == {"enabled": False}
    assert st.details["market_data"] == {"enabled": False, "reason": "market data not configured"}
    assert st.config_version == 1 and st.details["paper_guard"]["ok"] is True
    assert st.heartbeat_at is not None


@pytest.mark.parametrize("overrides", [
    {"trading_mode": "LIVE"},
    {"alpaca_base_url": "https://api.alpaca.markets"},
])
async def test_paper_guard_failure_blocks_and_refuses_start(
        env: EnvSettings, sessionmaker: Any, overrides: dict[str, str]) -> None:
    rt = make_runtime(env, sessionmaker, **overrides)
    await rt.boot()
    st = await state(sessionmaker)
    assert st.status == "EMERGENCY STOP"
    assert st.blocks[0]["reason"] == "CONFIG_ERROR"
    assert st.details["paper_guard"]["ok"] is False
    async with transaction(sessionmaker) as s:
        cmd = await engine_repo.enqueue_command(s, command="START", user_id=None)
    await rt.process_commands_once()
    async with sessionmaker() as s:
        done = await s.get(EngineCommand, cmd.id)
    assert done.status == "FAILED" and rt.trading_enabled is False


async def test_start_and_pause_commands(env: EnvSettings, sessionmaker: Any) -> None:
    rt = make_runtime(env, sessionmaker)
    await rt.boot()
    async with transaction(sessionmaker) as s:
        start = await engine_repo.enqueue_command(s, command="START", user_id=None)
    assert await rt.process_commands_once() == 1
    st = await state(sessionmaker)
    assert st.trading_enabled is True
    # Entries remain blocked: no broker, no market data, no entry window.
    assert [b["reason"] for b in st.blocks] == ["NO_BROKER", "DATA_STALE",
                                                "OUTSIDE_ENTRY_WINDOW"]
    assert rt.gate.entries_allowed is False
    async with sessionmaker() as s:
        done = await s.get(EngineCommand, start.id)
    assert done.status == "DONE" and done.result["entries_allowed"] is False

    async with transaction(sessionmaker) as s:
        await engine_repo.enqueue_command(s, command="PAUSE", user_id=None)
        unsupported = await engine_repo.enqueue_command(s, command="CLOSE_ALL", user_id=None)
    assert await rt.process_commands_once() == 2
    assert (await state(sessionmaker)).trading_enabled is False
    async with sessionmaker() as s:
        assert (await s.get(EngineCommand, unsupported.id)).status == "FAILED"


async def test_trading_enabled_and_latches_survive_restart(env: EnvSettings,
                                                           sessionmaker: Any) -> None:
    rt = make_runtime(env, sessionmaker)
    await rt.boot()
    rt.trading_enabled = True
    rt.gate.clear("PAUSED_BY_USER")
    rt.gate.set("DAILY_LOSS_LIMIT", rt.clock())
    await rt.write_state()

    rt2 = make_runtime(env, sessionmaker)
    await rt2.boot()
    assert rt2.trading_enabled is True
    assert rt2.gate.latched == ["DAILY_LOSS_LIMIT"]
    assert (await state(sessionmaker)).status == "DAILY LOSS LIMIT"
    async with transaction(sessionmaker) as s:
        await engine_repo.enqueue_command(s, command="START", user_id=None)
    await rt2.process_commands_once()
    async with sessionmaker() as s:
        cmd = (await s.execute(select(EngineCommand))).scalar_one()
    assert cmd.status == "FAILED" and "reset required" in cmd.result["error"]


async def test_settings_changes_are_applied(env: EnvSettings, sessionmaker: Any) -> None:
    rt = make_runtime(env, sessionmaker)
    await rt.boot()
    new = TradingConfig().to_json()
    new["risk"]["max_daily_loss"] = "400"
    new["data"]["feed"] = "sip"
    async with transaction(sessionmaker) as s:
        await settings_repo.activate_new(s, config=new, base_version=1, user_id=None,
                                         comment="t")
    await rt.check_settings_once()
    assert rt.config_version == 2 and rt.config is not None
    assert str(rt.config.risk.max_daily_loss) == "400"
    assert rt.pending_restart == ["data.feed"]
    await rt.write_state()
    assert (await state(sessionmaker)).details["pending_restart"] == ["data.feed"]


async def test_invalid_settings_are_rejected_and_previous_kept(env: EnvSettings,
                                                               sessionmaker: Any) -> None:
    rt = make_runtime(env, sessionmaker)
    await rt.boot()
    bad = TradingConfig().to_json()
    bad["risk"]["max_daily_loss"] = "99999"  # violates hard limit (3% of equity)
    bad["risk"]["max_loss_per_trade"] = "100"
    async with transaction(sessionmaker) as s:
        await settings_repo.activate_new(s, config=bad, base_version=1, user_id=None,
                                         comment="bad")
    await rt.check_settings_once()
    assert rt.config_version == 1
    assert "CONFIG_ERROR" not in rt.gate.blocks
    async with sessionmaker() as s:
        types = list((await s.execute(select(SystemEvent.type))).scalars())
    assert "SETTINGS_REJECTED" in types


async def test_db_outage_sets_and_clears_block(env: EnvSettings, sessionmaker: Any,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    rt = make_runtime(env, sessionmaker)
    await rt.boot()

    async def broken() -> None:
        raise OSError("connection refused")

    real = rt.write_state
    monkeypatch.setattr(rt, "write_state", broken)
    await rt.heartbeat_once()
    assert "DB_DOWN" in rt.gate.blocks and rt.gate.display_status() == "BROKER ERROR"
    monkeypatch.setattr(rt, "write_state", real)
    await rt.heartbeat_once()
    assert "DB_DOWN" not in rt.gate.blocks


async def test_run_loop_processes_commands_until_stopped(
        env: EnvSettings, sessionmaker: Any, fake_redis: Any) -> None:
    rt = make_runtime(env, sessionmaker, redis=fake_redis)
    stop = asyncio.Event()
    task = asyncio.create_task(rt.run(stop))
    for _ in range(100):
        if (await state(sessionmaker)) is not None:
            break
        await asyncio.sleep(0.02)
    async with transaction(sessionmaker) as s:
        await engine_repo.enqueue_command(s, command="START", user_id=None)
    await fake_redis.publish("commands:new", "{}")
    for _ in range(100):
        if rt.trading_enabled:
            break
        await asyncio.sleep(0.02)
    assert rt.trading_enabled
    assert await fake_redis.get("engine:heartbeat") is not None
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    async with sessionmaker() as s:
        types = list((await s.execute(select(SystemEvent.type))).scalars())
    assert types[0] == "ENGINE_STARTED" and types[-1] == "ENGINE_STOPPED"


async def test_api_start_pause_and_status(auth_client: httpx.AsyncClient, env: EnvSettings,
                                          sessionmaker: Any) -> None:
    status = (await auth_client.get(f"{API}/status")).json()
    assert status["trading_mode"] == "PAPER" and status["engine_online"] is False
    assert status["blocks"][0]["reason"] == "ENGINE_OFFLINE"

    rt = make_runtime(env, sessionmaker)
    await rt.boot()
    resp = await auth_client.post(f"{API}/controls/start")
    assert resp.status_code == 202
    cmd_id = resp.json()["command_id"]
    await rt.process_commands_once()
    cmd = (await auth_client.get(f"{API}/commands/{cmd_id}")).json()
    assert cmd["status"] == "DONE"

    status = (await auth_client.get(f"{API}/status")).json()
    assert status["engine_online"] is True and status["trading_enabled"] is True
    assert status["status"] == "BROKER ERROR"  # no broker or market data in tests
    assert status["active_config_version"] == status["running_config_version"] == 1
    assert status["ai_mode"] == "OFF" and status["data_feed"] == "iex"
    assert len((await auth_client.get(f"{API}/commands")).json()) == 1


async def test_api_start_refused_when_latched(auth_client: httpx.AsyncClient,
                                              sessionmaker: Any) -> None:
    async with transaction(sessionmaker) as s:
        await engine_repo.upsert_state(s, status="EMERGENCY STOP",
                                       latched=["EMERGENCY_STOP"], trading_enabled=False)
    resp = await auth_client.post(f"{API}/controls/start")
    assert resp.status_code == 409


async def test_engine_and_api_initialise_settings_once(env: EnvSettings,
                                                       sessionmaker: Any) -> None:
    async def init() -> None:
        async with transaction(sessionmaker) as s:
            await settings_repo.ensure_initial(s, TradingConfig())

    await asyncio.gather(*(init() for _ in range(5)))
    async with sessionmaker() as s:
        rows = (await s.execute(select(SettingsVersion))).scalars().all()
    assert len(rows) == 1


async def test_restarts_leave_no_gaps_in_version_numbers(sessionmaker: Any) -> None:
    for _ in range(3):  # e.g. api and engine restarting
        async with transaction(sessionmaker) as s:
            await settings_repo.ensure_initial(s, TradingConfig())
    new = TradingConfig().to_json()
    new["risk"]["max_daily_loss"] = "400"
    async with transaction(sessionmaker) as s:
        saved = await settings_repo.activate_new(s, config=new, base_version=1, user_id=None,
                                                 comment="t")
    assert saved.version == 2
