"""Universe scanner end-to-end: DB inputs + mocked Alpaca quotes → persisted reasons → API."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from intraday.core.env import EnvSettings
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import Bar1d, Bar1m, EngineCommand, UniverseScan
from intraday.db.repositories import engine as engine_repo
from intraday.db.session import transaction
from intraday.engine.runtime import EngineRuntime
from intraday.marketdata.alpaca.http import DATA_BASE_URL
from intraday.marketdata.scanner import UniverseScanner
from tests.integration.test_market_data_service import alpaca, make_service, seed  # noqa: F401

API = "/api/v1"
NOW = datetime(2026, 9, 21, 15, 30, tzinfo=UTC)   # Monday 11:30 ET (market open)


async def add_history(sm: Any) -> None:
    """20 prior sessions of daily bars; today's last 30 minutes of 1-min bars for AAPL."""
    async with transaction(sm) as s:
        for i in range(20):
            d = date(2026, 8, 21) + timedelta(days=i)
            for sym in ("AAPL", "TQQQ"):
                s.add(Bar1d(symbol=sym, feed="iex", session_date=d, open=Decimal("228"),
                            high=Decimal("230"), low=Decimal("227"), close=Decimal("229"),
                            volume=60_000))
        for m in range(1, 31):
            s.add(Bar1m(symbol="AAPL", feed="iex", ts=NOW - timedelta(minutes=m),
                        open=Decimal("229"), high=Decimal("229.5"), low=Decimal("228.8"),
                        close=Decimal("229.2"), volume=800))


def quotes_route(router: respx.MockRouter) -> respx.Route:
    return router.get(f"{DATA_BASE_URL}/v2/stocks/quotes/latest").mock(
        return_value=httpx.Response(200, json={"quotes": {"AAPL": {
            "t": (NOW - timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
            "bp": 229.18, "bs": 2, "ap": 229.22, "as": 3}}}))


async def ready_scanner(sm: Any, router: respx.MockRouter) -> UniverseScanner:
    await seed(sm)
    await add_history(sm)
    svc = make_service(sm, NOW)
    await svc.sync_calendar()
    await svc.sync_assets()
    quotes_route(router)
    return UniverseScanner(sm, svc, clock=lambda: NOW)


async def test_scan_records_every_reason(sessionmaker: Any, alpaca: Any) -> None:  # noqa: F811
    scanner = await ready_scanner(sessionmaker, alpaca)
    summary = await scanner.run(TradingConfig(), config_version=1)
    assert summary.passed == ["AAPL"]
    assert set(summary.failed) == {"GONE", "TQQQ"}
    assert "U2" in summary.failed["TQQQ"]              # name contains "3x" (E7)
    assert {"U2", "U3", "U4"} <= set(summary.failed["GONE"])  # not found, no data
    assert scanner.passed == frozenset({"AAPL"})

    async with sessionmaker() as s:
        rows = (await s.execute(select(UniverseScan))).scalars().all()
    assert {r.symbol for r in rows} == {"AAPL", "TQQQ", "GONE"}  # context SPY never scanned
    assert len({r.scan_id for r in rows}) == 1
    aapl = next(r for r in rows if r.symbol == "AAPL")
    assert aapl.passed and aapl.reasons["U6"]["evaluated"] is True
    assert aapl.metrics["adv"] == 60_000 and aapl.feed == "iex" and aapl.config_version == 1


async def test_quote_failure_fails_spread_but_scan_completes(sessionmaker: Any,
                                                            alpaca: Any) -> None:  # noqa: F811
    scanner = await ready_scanner(sessionmaker, alpaca)
    alpaca.get(f"{DATA_BASE_URL}/v2/stocks/quotes/latest").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"}))
    summary = await scanner.run(TradingConfig(), config_version=1)
    assert summary.quotes_error and "403" in summary.quotes_error
    assert summary.failed["AAPL"] == ["U6"]


async def test_scanner_api_and_run_command(auth_client: httpx.AsyncClient, sessionmaker: Any,
                                           alpaca: Any) -> None:  # noqa: F811
    assert (await auth_client.get(f"{API}/scanner")).json() == {"scan": None, "results": []}
    scanner = await ready_scanner(sessionmaker, alpaca)
    await scanner.run(TradingConfig(), config_version=1)

    body = (await auth_client.get(f"{API}/scanner")).json()
    assert body["scan"]["passed"] == 1 and body["scan"]["failed"] == 2
    assert body["results"][0]["symbol"] == "AAPL"  # passed first
    tqqq = next(r for r in body["results"] if r["symbol"] == "TQQQ")
    # Ineligible (E7), and also no bars/quote today; every reason is reported.
    assert tqqq["failed"] == ["U2", "U6", "U7", "U9"]
    assert "E7" in tqqq["reasons"]["U2"]["detail"]

    hist = (await auth_client.get(f"{API}/scanner/history", params={"symbol": "aapl"})).json()
    assert len(hist) == 1 and hist[0]["passed"] is True

    queued = await auth_client.post(f"{API}/scanner/run")
    assert queued.status_code == 202
    async with sessionmaker() as s:
        assert (await s.execute(select(EngineCommand.command))).scalar_one() == "SCAN"


async def test_engine_scans_at_boot_on_schedule_and_on_command(
        env: EnvSettings, sessionmaker: Any, alpaca: Any) -> None:  # noqa: F811
    await seed(sessionmaker)
    await add_history(sessionmaker)
    quotes_route(alpaca)
    clock = {"now": NOW}
    svc = make_service(sessionmaker, NOW)
    svc.clock = lambda: clock["now"]
    rt = EngineRuntime(env, sessionmaker, None, TradingConfig(),
                       market_factory=lambda cfg: (svc, None), clock=lambda: clock["now"])
    await rt.boot()
    await rt.wait_background()                     # boot: asset sync, then a first scan
    assert rt.scanner is not None and rt.scanner.last is not None
    first = rt.scanner.last.scan_id

    await rt.scan_tick()                           # not due yet (interval 5 min)
    assert rt.scanner.last.scan_id == first
    clock["now"] = NOW + timedelta(minutes=5)
    await rt.scan_tick()
    assert rt.scanner.last.scan_id != first

    async with transaction(sessionmaker) as s:
        cmd = await engine_repo.enqueue_command(s, command="SCAN", user_id=None)
    await rt.process_commands_once()
    await rt.wait_background()
    async with sessionmaker() as s:
        done = await s.get(EngineCommand, cmd.id)
    # Five minutes on, AAPL's quote is stale (U6). Its last bar closed exactly 5 min ago,
    # which is still within the no-trades limit, so U9 passes at this boundary.
    assert done.status == "DONE" and done.result["passed"] == []
    assert done.result["failed"]["AAPL"] == ["U6"]

    await rt.write_state()
    async with sessionmaker() as s:
        st = await engine_repo.get_state(s)
    assert st.details["universe"]["failed"]["AAPL"] == ["U6"]


@pytest.mark.parametrize("minute_offset", [0])
async def test_no_scheduled_scan_when_market_closed(env: EnvSettings, sessionmaker: Any,
                                                    alpaca: Any, minute_offset: int) -> None:  # noqa: F811
    await seed(sessionmaker)
    saturday = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
    svc = make_service(sessionmaker, saturday)
    rt = EngineRuntime(env, sessionmaker, None, TradingConfig(),
                       market_factory=lambda cfg: (svc, None), clock=lambda: saturday)
    await rt.boot()
    await rt.wait_background()
    boot_scan = rt.scanner.last  # boot scan always runs (reasons visible off-hours)
    await rt.scan_tick()
    assert rt.scanner.last is boot_scan
