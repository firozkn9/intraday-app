"""Replay with the live trading stack (SPEC §39): a full session end to end."""

from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from intraday.backtest.job import load_calendar
from intraday.core.runtime_config import TradingConfig
from intraday.db.models import AiDecision, Order, Position
from intraday.engine.demo import seed_demo
from intraday.engine.replay import ReplayDatabaseError, check_database, replay_session

DAY = date(2026, 9, 21)


def test_replay_refuses_the_live_database() -> None:
    with pytest.raises(ReplayDatabaseError):
        check_database("postgresql+asyncpg://u:p@db/intraday")
    assert check_database("postgresql+asyncpg://u:p@db/intraday_sim") == "intraday_sim"
    assert check_database("postgresql+asyncpg://u:p@db/intraday", allow_any=True)


async def test_full_session_replay_trades_and_ends_flat(sessionmaker: Any) -> None:
    await seed_demo(sessionmaker, DAY)
    cal = await load_calendar(sessionmaker, DAY - timedelta(days=40), DAY)
    lines: list[str] = []
    report = await replay_session(sessionmaker, cal, DAY, TradingConfig(), 1, ["AAA"],
                                  on_event=lines.append)
    assert report.minutes == 390 and report.flat_at_close
    assert report.submitted >= 1 and report.trades, lines
    first = report.trades[0]
    assert first["symbol"] == "AAA" and first["exit_reason"] in (
        "TAKE_PROFIT", "TRAILING_STOP", "TIME_EXIT", "STOP_LOSS", "MOMENTUM_REVERSAL",
        "VWAP_LOSS", "EOD_LIQUIDATION")
    async with sessionmaker() as s:
        positions = list((await s.execute(select(Position))).scalars())
        orders = list((await s.execute(select(Order))).scalars())
        decisions = list((await s.execute(select(AiDecision).where(
            AiDecision.decision_result == "ORDER_SUBMITTED"))).scalars())
    assert all(p.status in ("CLOSED", "ABANDONED") for p in positions)
    assert {o.intent for o in orders} >= {"ENTRY", "TAKE_PROFIT", "STOP_LOSS"}
    assert len(decisions) == report.submitted
    # Every entry was a bracket, and no entry left the account holding shares.
    assert all(o.order_class == "bracket" for o in orders if o.intent == "ENTRY")
