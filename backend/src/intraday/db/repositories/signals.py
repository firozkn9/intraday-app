"""Trading signals and AI decisions (the decision audit chain, docs/design/04 §2)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from intraday.db.models import AiDecision, Bar1d, Bar1m, RiskEvaluation, TradingSignal


async def insert_signals(session: AsyncSession, signals: Sequence[dict[str, Any]],
                         decisions: Sequence[dict[str, Any]]) -> None:
    if signals:
        await session.execute(insert(TradingSignal).values(list(signals)))
    if decisions:
        await session.execute(insert(AiDecision).values(list(decisions)))


async def list_signals(session: AsyncSession, *, symbol: str | None, session_date: date | None,
                       strategy: str | None, action: str | None, limit: int
                       ) -> list[TradingSignal]:
    stmt = select(TradingSignal).order_by(TradingSignal.bar_ts.desc(), TradingSignal.symbol)
    if symbol:
        stmt = stmt.where(TradingSignal.symbol == symbol)
    if session_date:
        stmt = stmt.where(TradingSignal.session_date == session_date)
    if strategy:
        stmt = stmt.where(TradingSignal.strategy == strategy)
    if action:
        stmt = stmt.where(TradingSignal.action == action)
    return list((await session.execute(stmt.limit(limit))).scalars())


async def list_decisions(session: AsyncSession, *, symbol: str | None, start: datetime | None,
                         end: datetime | None, result: str | None, strategy: str | None,
                         limit: int) -> list[AiDecision]:
    stmt = select(AiDecision).order_by(AiDecision.ts.desc())
    if symbol:
        stmt = stmt.where(AiDecision.symbol == symbol)
    if start:
        stmt = stmt.where(AiDecision.ts >= start)
    if end:
        stmt = stmt.where(AiDecision.ts < end)
    if result:
        stmt = stmt.where(AiDecision.decision_result == result)
    if strategy:
        stmt = stmt.where(AiDecision.strategy == strategy)
    return list((await session.execute(stmt.limit(limit))).scalars())


async def get_decision(session: AsyncSession, decision_id: uuid.UUID
                       ) -> tuple[AiDecision, TradingSignal, RiskEvaluation | None] | None:
    dec = await session.get(AiDecision, decision_id)
    if dec is None:
        return None
    sig = await session.get(TradingSignal, dec.signal_id)
    if sig is None:
        return None
    risk = (await session.execute(select(RiskEvaluation).where(
        RiskEvaluation.decision_id == dec.id))).scalar_one_or_none()
    return dec, sig, risk


async def minute_bars(session: AsyncSession, feed: str, symbols: Sequence[str],
                      start: datetime, end: datetime) -> list[Bar1m]:
    """Bars with start <= ts <= end (inclusive: `end` is the last completed bar)."""
    if not symbols:
        return []
    res = await session.execute(
        select(Bar1m).where(Bar1m.feed == feed, Bar1m.symbol.in_(symbols), Bar1m.ts >= start,
                            Bar1m.ts <= end).order_by(Bar1m.symbol, Bar1m.ts))
    return list(res.scalars())


async def daily_stats(session: AsyncSession, feed: str, symbols: Sequence[str], start: date,
                      before: date) -> list[Bar1d]:
    if not symbols:
        return []
    res = await session.execute(
        select(Bar1d).where(Bar1d.feed == feed, Bar1d.symbol.in_(symbols),
                            Bar1d.session_date >= start, Bar1d.session_date < before))
    return list(res.scalars())


CENTS = Decimal("0.01")


def money(x: float) -> Decimal:
    return Decimal(str(round(x, 4)))
