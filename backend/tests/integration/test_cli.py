"""CLI: create-admin and openapi."""

import json

import pytest
from typer.testing import CliRunner

from intraday.cli import app
from intraday.core import env as env_module

runner = CliRunner()


@pytest.fixture
def cli_env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    env_module.get_env.cache_clear()
    yield
    env_module.get_env.cache_clear()


def test_create_admin_once(cli_env: None, db_engine: object) -> None:
    args = ["create-admin", "--email", "Owner@Example.com", "--password-stdin"]
    ok = runner.invoke(app, args, input="a-strong-password-1\n")
    assert ok.exit_code == 0, ok.output
    assert "owner@example.com created" in ok.output
    again = runner.invoke(app, args, input="a-strong-password-2\n")
    assert again.exit_code == 1 and "already exists" in again.output


def test_create_admin_rejects_weak_password(cli_env: None, db_engine: object) -> None:
    res = runner.invoke(app, ["create-admin", "--email", "a@b.co", "--password-stdin"],
                        input="short\n")
    assert res.exit_code == 1 and "Weak password" in res.output


def test_openapi_lists_phase1_routes() -> None:
    res = runner.invoke(app, ["openapi"])
    assert res.exit_code == 0
    paths = json.loads(res.output)["paths"]
    for p in ("/api/v1/auth/login", "/api/v1/settings", "/api/v1/status",
              "/api/v1/controls/start", "/api/v1/health"):
        assert p in paths


def test_adv_report_prints_feed_adv_per_tradable_symbol(cli_env: None, db_engine: object,
                                                        db_url: str) -> None:
    import asyncio
    from datetime import date, timedelta
    from decimal import Decimal

    from intraday.db.models import Bar1d, Symbol
    from intraday.db.session import make_engine, make_sessionmaker, transaction

    async def setup() -> None:
        engine = make_engine(db_url, pool_size=1)
        async with transaction(make_sessionmaker(engine)) as s:
            s.add(Symbol(symbol="AAPL", role="TRADABLE", asset_type="COMMON_STOCK", country="US"))
            s.add(Symbol(symbol="SPY", role="CONTEXT", asset_type="ETF", country="US"))
            for i in range(5):
                s.add(Bar1d(symbol="AAPL", feed="iex", session_date=date(2026, 9, 1)
                            + timedelta(days=i), open=Decimal(1), high=Decimal(1),
                            low=Decimal(1), close=Decimal(1), volume=10_000 * (i + 1)))
        await engine.dispose()

    asyncio.run(setup())
    res = runner.invoke(app, ["adv-report", "--days", "5"])
    assert res.exit_code == 0, res.output
    assert "feed=iex" in res.output and "AAPL" in res.output and "SPY" not in res.output
    assert "threshold=20,000" in res.output  # defaults.yaml when no settings version exists
    assert "adv=       30000 PASS" in res.output
