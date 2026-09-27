"""Backtests end-to-end: stored data → simulation → persisted results → API and CLI."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from intraday.api.context import AppContext
from intraday.backtest.job import BacktestParams, coverage_gaps, execute_run, run_backtest
from intraday.core import env as env_module
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import BacktestRun, BacktestTrade
from tests.integration.test_decisions import seed_history
from tests.integration.test_risk_integration import add_symbol

API = "/api/v1"
TEST_DAY = {"start": "2026-09-21", "end": "2026-09-21"}


async def seed(sm: Any) -> None:
    await seed_history(sm)
    await add_symbol(sm)


async def test_run_backtest_from_stored_data(sessionmaker: Any) -> None:
    await seed(sessionmaker)
    params = BacktestParams.model_validate({"test": TEST_DAY, "symbols": ["AAA"]})
    assert await coverage_gaps(sessionmaker, params, "iex") == []
    res = await run_backtest(sessionmaker, params, TradingConfig(), feed="iex")
    m = res["TEST"]["metrics"]
    assert m["sessions"] == 1 and m["symbols"] == ["AAA"]
    assert m["trades"] == len(res["TEST"]["trades"]) >= 1
    assert res["TEST"]["curve"]


async def test_params_validation() -> None:
    with pytest.raises(ValueError, match="at least one"):
        BacktestParams.model_validate({})
    with pytest.raises(ValueError, match="must start after"):
        BacktestParams.model_validate({"train": {"start": "2026-09-01", "end": "2026-09-10"},
                                       "test": {"start": "2026-09-05", "end": "2026-09-20"}})


async def test_api_lifecycle(auth_client: httpx.AsyncClient, ctx: AppContext,
                             sessionmaker: Any) -> None:
    await seed(sessionmaker)
    launched: list[uuid.UUID] = []
    ctx.launch_backtest = launched.append

    resp = await auth_client.post(f"{API}/backtests", json={"test": TEST_DAY,
                                                            "symbols": ["AAA"]})
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["id"]
    assert [str(x) for x in launched] == [run_id]

    busy = await auth_client.post(f"{API}/backtests", json={"test": TEST_DAY})
    assert busy.status_code == 409                # one run at a time

    await execute_run(sessionmaker, uuid.UUID(run_id))
    run = (await auth_client.get(f"{API}/backtests/{run_id}")).json()
    assert run["status"] == "DONE" and run["metrics"]["TEST"]["sessions"] == 1
    assert run["config_snapshot"]["risk"]["max_daily_loss"] == "500"
    listed = (await auth_client.get(f"{API}/backtests")).json()
    assert listed[0]["summary"]["TEST"]["trades"] == run["metrics"]["TEST"]["trades"]
    trades = (await auth_client.get(f"{API}/backtests/{run_id}/trades",
                                    params={"split": "TEST"})).json()
    assert len(trades) == run["metrics"]["TEST"]["trades"]
    curve = (await auth_client.get(f"{API}/backtests/{run_id}/equity")).json()
    assert curve and float(curve[-1]["equity"]) == pytest.approx(
        run["metrics"]["TEST"]["final_equity"], abs=0.01)
    assert (await auth_client.post(f"{API}/backtests/{run_id}/cancel")).status_code == 409


async def test_api_rejects_missing_data_and_bad_input(auth_client: httpx.AsyncClient,
                                                      sessionmaker: Any) -> None:
    await seed(sessionmaker)
    gap = await auth_client.post(f"{API}/backtests", json={
        "test": {"start": "2026-10-05", "end": "2026-10-09"}})
    assert gap.status_code == 422
    assert "no market-calendar sessions" in gap.json()["errors"][0]["message"]
    bad = await auth_client.post(f"{API}/backtests", json={"test": TEST_DAY, "bogus": 1})
    assert bad.status_code == 422
    over = await auth_client.post(f"{API}/backtests", json={
        "test": TEST_DAY, "config_overrides": {"risk": {"max_open_positions": 99}}})
    assert over.status_code == 422 and over.json()["errors"][0]["key"] == "risk.max_open_positions"


async def test_cancel_before_start(auth_client: httpx.AsyncClient, ctx: AppContext,
                                   sessionmaker: Any) -> None:
    await seed(sessionmaker)
    ctx.launch_backtest = lambda _id: None
    run_id = (await auth_client.post(f"{API}/backtests", json={"test": TEST_DAY})).json()["id"]
    assert (await auth_client.post(f"{API}/backtests/{run_id}/cancel")).json()["status"] == \
        "CANCELED"
    await execute_run(sessionmaker, uuid.UUID(run_id))   # nothing to do
    async with sessionmaker() as s:
        assert (await s.get(BacktestRun, uuid.UUID(run_id))).status == "CANCELED"
        assert not (await s.execute(select(BacktestTrade))).scalars().all()


def _cli(db_url: str, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> Any:
    from intraday.cli import app

    monkeypatch.setenv("DATABASE_URL", db_url)
    env_module.get_env.cache_clear()
    try:
        return CliRunner().invoke(app, args)
    finally:
        env_module.get_env.cache_clear()


def _seed_sync(db_url: str) -> None:
    from intraday.db.session import make_engine, make_sessionmaker

    async def go() -> None:
        engine = make_engine(db_url, pool_size=1)
        await seed(make_sessionmaker(engine))
        await engine.dispose()

    asyncio.run(go())


def test_cli_replay_and_backtest(db_url: str, sessionmaker: Any,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_sync(db_url)
    rep = _cli(db_url, monkeypatch, ["replay", "--date", "2026-09-21", "--symbol", "AAA"])
    assert rep.exit_code == 0, rep.output
    assert "replay 2026-09-21 feed=iex symbols=1" in rep.output
    assert "ENTRY" in rep.output and "trades=" in rep.output

    bt = _cli(db_url, monkeypatch, ["backtest", "--test", "2026-09-21", "--symbol", "AAA"])
    assert bt.exit_code == 0, bt.output
    assert "[TEST]" in bt.output and "total_return_pct" in bt.output

    persisted = _cli(db_url, monkeypatch, ["backtest", "--test", "2026-09-21", "--persist"])
    assert persisted.exit_code == 0 and ": DONE" in persisted.output

    missing = _cli(db_url, monkeypatch, ["backtest", "--test", "2026-10-05:2026-10-09"])
    assert missing.exit_code == 1 and "Not enough data" in missing.output


def test_worker_process_entry_point(db_url: str, sessionmaker: Any) -> None:
    """``run_in_process`` is what the API's worker process executes."""
    from intraday.backtest.job import run_in_process
    from intraday.db.session import make_engine, make_sessionmaker, transaction

    _seed_sync(db_url)

    async def create() -> uuid.UUID:
        engine = make_engine(db_url, pool_size=1)
        try:
            async with transaction(make_sessionmaker(engine)) as s:
                row = BacktestRun(status="QUEUED", feed="iex",
                                  params=BacktestParams.model_validate(
                                      {"test": TEST_DAY}).model_dump(mode="json"),
                                  config_snapshot=TradingConfig().to_json())
                s.add(row)
                await s.flush()
                return row.id
        finally:
            await engine.dispose()

    run_id = asyncio.run(create())
    run_in_process(db_url, str(run_id))

    async def status() -> str:
        engine = make_engine(db_url, pool_size=1)
        try:
            async with make_sessionmaker(engine)() as s:
                return (await s.get(BacktestRun, run_id)).status  # type: ignore[union-attr]
        finally:
            await engine.dispose()

    assert asyncio.run(status()) == "DONE"
