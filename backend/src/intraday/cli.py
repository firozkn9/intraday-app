"""``intraday`` command-line interface."""

from __future__ import annotations

import asyncio
import getpass
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import typer

from intraday.core.env import get_env
from intraday.core.timeutil import ET

app = typer.Typer(no_args_is_help=True, help="Intraday App operations (paper trading only).")

ENGINE_HEALTHY_WITHIN_SECONDS = 15


@app.command("create-admin")
def create_admin(
    email: str = typer.Option(..., prompt=True, help="Admin email"),
    password_stdin: bool = typer.Option(False, help="Read the password from stdin"),
) -> None:
    """Create the single admin account (refuses if any user exists)."""
    from intraday.api.security import passwords
    from intraday.db.repositories import users as users_repo
    from intraday.db.session import make_engine, make_sessionmaker, transaction

    if password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != password:
            typer.echo("Passwords do not match.", err=True)
            raise typer.Exit(1)
    try:
        passwords.check_policy(password, email=email)
    except passwords.WeakPassword as exc:
        typer.echo(f"Weak password: {exc}", err=True)
        raise typer.Exit(1) from exc

    async def run() -> None:
        engine = make_engine(get_env().database_url)
        try:
            async with transaction(make_sessionmaker(engine)) as s:
                if await users_repo.count_users(s) > 0:
                    typer.echo("An admin account already exists; refusing.", err=True)
                    raise typer.Exit(1)
                user = await users_repo.create_user(s, email, passwords.hash_password(password))
                await users_repo.audit(s, action="ADMIN_CREATED", user_id=user.id,
                                       target=user.email)
        finally:
            await engine.dispose()

    asyncio.run(run())
    typer.echo(f"Admin {email.lower()} created.")


@app.command()
def healthcheck() -> None:
    """Exit 0 if the engine heartbeat is fresh (used by the engine container)."""
    from intraday.core.timeutil import utcnow
    from intraday.db.repositories import engine as engine_repo
    from intraday.db.session import make_engine, make_sessionmaker

    async def run() -> bool:
        engine = make_engine(get_env().database_url, pool_size=1)
        try:
            async with make_sessionmaker(engine)() as s:
                state = await engine_repo.get_state(s)
        finally:
            await engine.dispose()
        return bool(state and state.heartbeat_at and
                    (utcnow() - state.heartbeat_at).total_seconds()
                    <= ENGINE_HEALTHY_WITHIN_SECONDS)

    raise typer.Exit(0 if asyncio.run(run()) else 1)


@app.command("db-upgrade")
def db_upgrade(revision: str = typer.Argument("head")) -> None:
    """Run Alembic migrations (uses DATABASE_URL_OWNER or DATABASE_URL)."""
    from alembic import command
    from alembic.config import Config

    # Working directory in the container (/app), else the source checkout.
    candidates = [Path.cwd() / "alembic.ini", Path(__file__).resolve().parents[2] / "alembic.ini"]
    ini = next((p for p in candidates if p.is_file()), None)
    if ini is None:
        typer.echo("alembic.ini not found; run from the backend directory.", err=True)
        raise typer.Exit(1)
    command.upgrade(Config(str(ini)), revision)


@app.command()
def backfill(days: int = typer.Option(30, min=1, max=60, help="Sessions to cover"),
             full: bool = typer.Option(False, help="Re-fetch everything in the window")) -> None:
    """Pull historical 1-min and daily bars for the watchlist (run where the Alpaca keys are)."""
    from intraday.core.runtime_config import parse_config
    from intraday.db.repositories import settings as settings_repo
    from intraday.db.session import make_engine, make_sessionmaker
    from intraday.marketdata.factory import build_market_data

    async def run() -> None:
        env = get_env()
        engine = make_engine(env.database_url, pool_size=2)
        sm = make_sessionmaker(engine)
        async with sm() as s:
            active = await settings_repo.get_active(s)
        feed = parse_config(active.config).data.feed if active else env.alpaca_data_feed_default
        service, reason = build_market_data(env, sm, feed=feed)
        if service is None:
            typer.echo(f"Market data unavailable: {reason}", err=True)
            await engine.dispose()
            raise typer.Exit(1)
        try:
            await service.sync_calendar()
            plan = await service.refresh_watchlist()
            eligible = await service.sync_assets()
            report = await service.backfill(days, full=full)
        finally:
            await service.aclose()
            await engine.dispose()
        typer.echo(json.dumps({"feed": service.feed, "streamed_symbols": plan.subscribed,
                               "not_streamed": plan.dropped,
                               "ineligible": [k for k, v in eligible.items() if not v],
                               "report": report.to_json()}, indent=2))

    asyncio.run(run())


@app.command("adv-report")
def adv_report(days: int = typer.Option(20, min=5, max=60, help="Sessions to average")) -> None:
    """Average daily volume per tradable symbol on the configured feed (no API keys needed).

    Use it to calibrate universe.min_average_daily_volume after a backfill: on IEX the
    numbers are a small fraction (~2-3%) of consolidated volume.
    """
    from sqlalchemy import select

    from intraday.core.runtime_config import load_defaults, parse_config
    from intraday.db.models import Bar1d, Symbol
    from intraday.db.repositories import settings as settings_repo
    from intraday.db.session import make_engine, make_sessionmaker

    async def run() -> None:
        engine = make_engine(get_env().database_url, pool_size=1)
        try:
            async with make_sessionmaker(engine)() as s:
                active = await settings_repo.get_active(s)
                cfg = (parse_config(active.config) if active
                       else load_defaults(get_env().defaults_path))
                feed = cfg.data.feed
                symbols = list((await s.execute(select(Symbol.symbol).where(
                    Symbol.role == "TRADABLE").order_by(Symbol.symbol))).scalars())
                rows = []
                for sym in symbols:
                    vols = list((await s.execute(
                        select(Bar1d.volume).where(Bar1d.symbol == sym, Bar1d.feed == feed)
                        .order_by(Bar1d.session_date.desc()).limit(days))).scalars())
                    rows.append((sym, len(vols), int(sum(vols) / len(vols)) if vols else None))
        finally:
            await engine.dispose()
        threshold = cfg.universe.min_average_daily_volume
        typer.echo(f"feed={feed} lookback={days} sessions threshold={threshold:,}")
        for sym, n, adv in rows:
            flag = "" if adv is None else (" PASS" if adv >= threshold else " below threshold")
            typer.echo(f"  {sym:<6} sessions={n:<3} adv={adv if adv is not None else '-':>12}"
                       f"{flag}")

    asyncio.run(run())


@app.command()
def signals(day: str = typer.Option(..., "--date", help="Session date YYYY-MM-DD"),
            symbol: list[str] = typer.Option(None, help="Limit to these symbols (repeatable)"),
            show_all: bool = typer.Option(False, "--all", help="Also list non-trades")) -> None:
    """Replay the signal pipeline over a stored session (dry run: nothing is written)."""
    from datetime import date as date_cls

    from sqlalchemy import select

    from intraday.ai.pipeline import replay_session
    from intraday.core.runtime_config import load_defaults, parse_config
    from intraday.db.models import Symbol
    from intraday.db.repositories import marketdata as md_repo
    from intraday.db.repositories import settings as settings_repo
    from intraday.db.session import make_engine, make_sessionmaker
    from intraday.engine.decisions import load_contexts, load_session_bars
    from intraday.features.regime import REFERENCE

    session_date = date_cls.fromisoformat(day)

    async def run() -> None:
        engine = make_engine(get_env().database_url, pool_size=2)
        sm = make_sessionmaker(engine)
        try:
            async with sm() as s:
                active = await settings_repo.get_active(s)
                cfg = (parse_config(active.config) if active
                       else load_defaults(get_env().defaults_path))
                version = active.version if active else 0
                calendar = await md_repo.load_calendar(s, session_date - timedelta(days=60),
                                                       session_date)
                universe = [x.upper() for x in symbol] if symbol else list((await s.execute(
                    select(Symbol.symbol).where(Symbol.role == "TRADABLE", Symbol.enabled)
                    .order_by(Symbol.symbol))).scalars())
            session = calendar.session(session_date)
            if session is None:
                typer.echo(f"{session_date} is not a stored trading session "
                           "(run a backfill or check the date).", err=True)
                raise typer.Exit(1)
            syms = sorted(set(universe) | set(REFERENCE))
            ctx = await load_contexts(sm, calendar, cfg.data.feed, session, syms,
                                      cfg.universe.adv_lookback_days)
            bars = await load_session_bars(sm, cfg.data.feed, session, syms,
                                           session.close_at)
        finally:
            await engine.dispose()
        cycles = replay_session(bars, ctx, session, universe, cfg, version)
        n_eval = sum(len(c.evaluated) for c in cycles)
        props = [r for c in cycles for r in c.proposals]
        typer.echo(f"{session_date} feed={cfg.data.feed} symbols={len(universe)} "
                   f"cycles={len(cycles)} evaluations={n_eval} proposals={len(props)}")
        for c in cycles:
            for r in c.records:
                if r.proposal is None and not show_all:
                    continue
                et = r.bar_ts.astimezone(ET).strftime("%H:%M")
                typer.echo(f"  {et} {r.symbol:<6} {r.strategy:<18} {r.action:<8} "
                           f"score={r.score:.2f} regime={r.regime}"
                           + (f"  ({r.note})" if r.note else ""))

    asyncio.run(run())


def _range(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    start, _, end = value.partition(":")
    return {"start": start, "end": end or start}


async def _active_config(sm: Any) -> tuple[Any, int]:
    from intraday.core.runtime_config import load_defaults, parse_config
    from intraday.db.repositories import settings as settings_repo

    async with sm() as s:
        active = await settings_repo.get_active(s)
    if active is None:
        return load_defaults(get_env().defaults_path), 0
    return parse_config(active.config), active.version


@app.command()
def backtest(train: str = typer.Option(None, help="START:END (YYYY-MM-DD)"),
             validation: str = typer.Option(None, help="START:END, after train"),
             test: str = typer.Option(None, help="START:END, after validation"),
             symbol: list[str] = typer.Option(None, help="Limit to these symbols"),
             capital: float = typer.Option(None, help="Starting capital (default: settings)"),
             slippage_bps: int = typer.Option(5, help="Slippage per fill, basis points"),
             persist: bool = typer.Option(False, help="Save the run (visible in the API/UI)"),
             ) -> None:
    """Backtest stored history with the live pipeline and risk engine; prints metrics."""
    from pydantic import ValidationError

    from intraday.backtest.costs import CostModel
    from intraday.backtest.job import (
        BacktestParams,
        coverage_gaps,
        effective_config,
        execute_run,
        run_backtest,
    )
    from intraday.db.models import BacktestRun
    from intraday.db.session import make_engine, make_sessionmaker, transaction

    try:
        params = BacktestParams.model_validate({
            "train": _range(train), "validation": _range(validation), "test": _range(test),
            "symbols": symbol or None, "starting_capital": capital,
            "costs": CostModel(slippage_bps=slippage_bps).model_dump()})
    except ValidationError as exc:
        typer.echo(f"Invalid parameters: {exc.errors()[0]['msg']}", err=True)
        raise typer.Exit(2) from exc

    async def run() -> None:
        engine = make_engine(get_env().database_url, pool_size=2)
        sm = make_sessionmaker(engine)
        try:
            base, version = await _active_config(sm)
            cfg = effective_config(base, params)
            gaps = await coverage_gaps(sm, params, cfg.data.feed)
            if gaps:
                typer.echo("Not enough data:\n  " + "\n  ".join(gaps), err=True)
                raise typer.Exit(1)
            if persist:
                async with transaction(sm) as s:
                    row = BacktestRun(status="QUEUED", params=params.model_dump(mode="json"),
                                      config_snapshot=cfg.to_json(), feed=cfg.data.feed)
                    s.add(row)
                    await s.flush()
                    run_id = row.id
                await execute_run(sm, run_id)
                async with sm() as s:
                    done = await s.get(BacktestRun, run_id)
                typer.echo(f"run {run_id}: {done.status if done else '?'}")
                metrics = (done.metrics or {}) if done else {}
            else:
                results = await run_backtest(sm, params, cfg, feed=cfg.data.feed,
                                             config_version=version)
                metrics = {k: v["metrics"] for k, v in results.items()}
        finally:
            await engine.dispose()
        keys = ("sessions", "trades", "total_return_pct", "annualized_return_pct",
                "max_drawdown_pct", "sharpe", "sortino", "win_rate_pct", "profit_factor",
                "average_win", "average_loss", "largest_loss", "largest_winning_streak",
                "largest_losing_streak", "average_holding_minutes", "net_pnl", "fees")
        for split, m in metrics.items():
            typer.echo(f"[{split}]")
            for k in keys:
                typer.echo(f"  {k:<26} {m.get(k)}")

    asyncio.run(run())


@app.command()
def replay(day: str = typer.Option(..., "--date", help="Session date YYYY-MM-DD"),
           symbol: list[str] = typer.Option(None, help="Limit to these symbols"),
           quiet: bool = typer.Option(False, help="Only the summary, not every event"),
           engine_mode: bool = typer.Option(
               False, "--engine", help="Drive the live trading stack (order manager, "
               "SimBroker, exits, EOD) and WRITE orders/positions to a simulation database"),
           allow_any_db: bool = typer.Option(False, help="With --engine: skip the "
                                             "simulation-database name check")) -> None:
    """Simulate one stored session minute by minute and print every event.

    Default: dry run with the backtest simulator (nothing written). ``--engine``: the
    real order manager, position manager, exit engine and liquidator against SimBroker.
    """
    from datetime import date as date_cls

    if engine_mode:
        _replay_engine(day, symbol or None, quiet, allow_any_db)
        return

    from intraday.backtest.costs import CostModel
    from intraday.backtest.job import iter_sessions, load_calendar, universe_info
    from intraday.backtest.metrics import summarize
    from intraday.backtest.runner import Event, Simulator
    from intraday.db.session import make_engine, make_sessionmaker

    session_date = date_cls.fromisoformat(day)

    def show(ev: Event) -> None:
        if not quiet:
            et = ev.ts.astimezone(ET).strftime("%H:%M")
            typer.echo(f"  {et} {ev.type:<15} {ev.symbol or '':<6} {ev.detail}")

    async def run() -> None:
        engine = make_engine(get_env().database_url, pool_size=2)
        sm = make_sessionmaker(engine)
        try:
            cfg, version = await _active_config(sm)
            cal = await load_calendar(sm, session_date - timedelta(days=60), session_date)
            session = cal.session(session_date)
            if session is None:
                typer.echo(f"{session_date} is not a stored trading session.", err=True)
                raise typer.Exit(1)
            symbols, elig, sectors = await universe_info(sm, symbol or None)
            costs = CostModel()
            sim = Simulator(cfg, costs, cfg.risk.paper_initial_balance, feed=cfg.data.feed,
                            eligibility=elig, sectors=sectors, config_version=version,
                            on_event=show)
            typer.echo(f"replay {session_date} feed={cfg.data.feed} symbols={len(symbols)}")
            async for sd in iter_sessions(sm, cfg.data.feed, cal, [session], symbols,
                                          cfg.universe.adv_lookback_days):
                sim.run_session(sd, symbols)
        finally:
            await engine.dispose()
        m = summarize(sim.trades, sim.daily_equity, float(cfg.risk.paper_initial_balance))
        typer.echo(f"trades={m['trades']} net_pnl={m['net_pnl']} fees={m['fees']} "
                   f"win_rate={m['win_rate_pct']} exits={m['exit_reasons']}")

    asyncio.run(run())


def _replay_engine(day: str, symbols: list[str] | None, quiet: bool, allow_any_db: bool) -> None:
    from datetime import date as date_cls

    from intraday.backtest.job import load_calendar, universe_info
    from intraday.db.session import make_engine, make_sessionmaker
    from intraday.engine.replay import ReplayDatabaseError, check_database, replay_session

    env = get_env()
    try:
        name = check_database(env.database_url, allow_any=allow_any_db)
    except ReplayDatabaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    session_date = date_cls.fromisoformat(day)

    async def run() -> None:
        engine = make_engine(env.database_url, pool_size=3)
        sm = make_sessionmaker(engine)
        try:
            cfg, version = await _active_config(sm)
            cal = await load_calendar(sm, session_date - timedelta(days=60), session_date)
            universe, _, _ = await universe_info(sm, symbols)
            typer.echo(f"engine replay {session_date} db={name} symbols={len(universe)}")
            report = await replay_session(
                sm, cal, session_date, cfg, version, universe, feed=cfg.data.feed,
                on_event=None if quiet else (lambda line: typer.echo(f"  {line}")))
        finally:
            await engine.dispose()
        typer.echo(json.dumps(report.to_json(), indent=2))
        if not report.flat_at_close:
            raise typer.Exit(1)

    asyncio.run(run())


@app.command("demo-seed")
def demo_seed(day: str = typer.Option("2026-09-21", "--date", help="Session to create"),
              allow_any_db: bool = typer.Option(False, help="Skip the database name check")
              ) -> None:
    """Write a deterministic synthetic session (for replay/E2E; simulation databases only).

    Creates 12 prior sessions of history plus one session where AAA breaks out and runs
    to its target, SPY/QQQ trend up, and the calendar rows for all of them.
    """
    from datetime import date as date_cls

    from intraday.db.session import make_engine, make_sessionmaker
    from intraday.engine.demo import seed_demo
    from intraday.engine.replay import ReplayDatabaseError, check_database

    env = get_env()
    try:
        check_database(env.database_url, allow_any=allow_any_db)
    except ReplayDatabaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    async def run() -> int:
        engine = make_engine(env.database_url, pool_size=2)
        try:
            return await seed_demo(make_sessionmaker(engine), date_cls.fromisoformat(day))
        finally:
            await engine.dispose()

    typer.echo(f"Wrote {asyncio.run(run())} bars.")


@app.command("seed-watchlist")
def seed_watchlist(path: Path = typer.Option(None, help="Seed YAML (default: config)")) -> None:
    """Insert seed symbols that aren't on the watchlist yet (never overwrites)."""
    from intraday.db.repositories import marketdata as md_repo
    from intraday.db.session import make_engine, make_sessionmaker, transaction
    from intraday.marketdata.watchlist import load_seed, validate_seed

    env = get_env()
    seed = load_seed(path or env.watchlist_seed_path)
    problems = validate_seed(seed, env.alpaca_stream_symbol_limit)
    if problems:
        typer.echo("Seed file problems:\n  " + "\n  ".join(problems), err=True)
        raise typer.Exit(1)

    async def run() -> int:
        engine = make_engine(env.database_url, pool_size=1)
        try:
            async with transaction(make_sessionmaker(engine)) as s:
                return await md_repo.seed_symbols(s, [e.to_row() for e in seed.all],
                                                  only_if_empty=False)
        finally:
            await engine.dispose()

    typer.echo(f"Added {asyncio.run(run())} symbol(s).")


@app.command()
def openapi(output: Path = typer.Option(Path("-"), help="File path, or - for stdout")) -> None:
    """Write the API's OpenAPI document (used to generate frontend types)."""
    from intraday.api.main import create_app

    doc = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
    if str(output) == "-":
        sys.stdout.write(doc)
    else:
        output.write_text(doc, encoding="utf-8")


if __name__ == "__main__":
    app()
