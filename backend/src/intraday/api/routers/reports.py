"""/dashboard, /trades, /calendar, /performance (docs/design/05 §3, §5, §8). Read-only."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, select

from intraday.analytics import performance as perf
from intraday.api.deps import DB, CurrentPrincipal, read_limit
from intraday.api.errors import Problem
from intraday.api.routers.positions import _order_json, _position_json
from intraday.core.runtime_config import parse_config
from intraday.core.timeutil import ET, utcnow
from intraday.db.models import (
    AccountSnapshot,
    AiDecision,
    Bar1m,
    DailyPnl,
    Fill,
    LlmCall,
    MarketCalendarDay,
    OrderEvent,
    Position,
    RiskEvaluation,
    TradingSignal,
)
from intraday.db.repositories import engine as engine_repo
from intraday.db.repositories import settings as settings_repo
from intraday.db.repositories import trading as trading_repo

router = APIRouter(tags=["reports"], dependencies=[Depends(read_limit)])
MAX_TRADES = 5000


def _today() -> date:
    return utcnow().astimezone(ET).date()


async def _start_equity(db: DB) -> Decimal:
    active = await settings_repo.get_active(db)
    return parse_config(active.config).risk.paper_initial_balance if active else Decimal(100000)


# ------------------------------------------------------------------ dashboard

@router.get("/dashboard/summary")
async def dashboard_summary(principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    today = _today()
    acct = (await db.execute(select(AccountSnapshot).order_by(AccountSnapshot.ts.desc())
                             .limit(1))).scalar_one_or_none()
    day = await db.get(DailyPnl, today)
    live = await trading_repo.live_positions(db)
    total = sum((Decimal(p.net_pnl or 0) for p in (await db.execute(
        select(Position).where(Position.status == "CLOSED"))).scalars()), Decimal(0))
    state = await engine_repo.get_state(db)
    dl = (state.details or {}).get("daily_loss") if state else None
    unrealized = Decimal(acct.unrealized_pnl) if acct else Decimal(0)
    realized = Decimal(day.realized_net) if day else Decimal(0)
    return {
        "account": None if acct is None else {
            "as_of": acct.ts.isoformat(), "equity": str(acct.equity), "cash": str(acct.cash),
            "buying_power": str(acct.buying_power), "exposure": str(acct.exposure)},
        "today": {"date": today.isoformat(), "pnl": str(realized + unrealized),
                  "realized": str(realized), "unrealized": str(unrealized),
                  "trades": day.trades if day else 0, "wins": day.wins if day else 0,
                  "losses": day.losses if day else 0,
                  "win_rate": float(day.win_rate) if day and day.win_rate is not None else None,
                  "daily_loss_limit": dl.get("limit") if dl else None,
                  "daily_loss_used": dl.get("loss_used") if dl else None},
        "total_pnl": str(total), "open_positions_count": len(live),
        "status": state.status if state else "UNKNOWN"}


@router.get("/dashboard/equity-intraday")
async def equity_intraday(principal: CurrentPrincipal, db: DB,
                          on: date | None = Query(None, alias="date")) -> list[dict[str, Any]]:
    d = on or _today()
    start = datetime.combine(d, time(4, 0), ET)
    rows = (await db.execute(select(AccountSnapshot).where(
        AccountSnapshot.ts >= start, AccountSnapshot.ts < start + timedelta(hours=16))
        .order_by(AccountSnapshot.ts))).scalars()
    return [{"ts": r.ts.isoformat(), "equity": str(r.equity), "daily_pnl": str(r.daily_pnl)}
            for r in rows]


# ------------------------------------------------------------------ trades

def _trade_query(date_from: date | None, date_to: date | None, symbol: str | None,
                 strategy: str | None, outcome: str | None, exit_reason: str | None,
                 pnl_min: float | None, pnl_max: float | None, confidence_min: float | None,
                 confidence_max: float | None) -> Select[Any]:
    q = select(Position).where(Position.status == "CLOSED")
    if date_from:
        q = q.where(Position.session_date >= date_from)
    if date_to:
        q = q.where(Position.session_date <= date_to)
    if symbol:
        q = q.where(Position.symbol == symbol.upper())
    if strategy:
        q = q.where(Position.strategy == strategy)
    if outcome == "win":
        q = q.where(Position.net_pnl > 0)
    elif outcome == "loss":
        q = q.where(Position.net_pnl <= 0)
    if exit_reason:
        q = q.where(Position.exit_reason == exit_reason)
    if pnl_min is not None:
        q = q.where(Position.net_pnl >= pnl_min)
    if pnl_max is not None:
        q = q.where(Position.net_pnl <= pnl_max)
    if confidence_min is not None:
        q = q.where(Position.score >= confidence_min)
    if confidence_max is not None:
        q = q.where(Position.score <= confidence_max)
    return q


def _hhmm(s: str | None) -> time | None:
    if not s:
        return None
    try:
        return time.fromisoformat(s)
    except ValueError as exc:
        raise Problem(422, "Invalid time", f"{s!r} is not HH:MM") from exc


async def _trades(db: DB, *, sort: str = "exit_desc", **f: Any) -> list[Position]:
    times = {k: _hhmm(f.pop(k)) for k in ("entry_after", "entry_before", "exit_after",
                                           "exit_before")}
    q = _trade_query(**f)
    order: Any = {"exit_desc": Position.exit_time.desc(), "exit_asc": Position.exit_time.asc(),
                  "pnl_desc": Position.net_pnl.desc(), "pnl_asc": Position.net_pnl.asc()}[sort]
    rows: list[Position] = list((await db.execute(q.order_by(order).limit(MAX_TRADES)))
                                .scalars())

    def ok(ts: datetime | None, after: time | None, before: time | None) -> bool:
        if ts is None:
            return after is None and before is None
        t = ts.astimezone(ET).time()
        return (after is None or t >= after) and (before is None or t <= before)

    return [p for p in rows if ok(p.entry_time, times["entry_after"], times["entry_before"])
            and ok(p.exit_time, times["exit_after"], times["exit_before"])]


def _filters(date_from: date | None = None, date_to: date | None = None,
             symbol: str | None = None, strategy: str | None = None,
             outcome: Literal["win", "loss"] | None = None, exit_reason: str | None = None,
             pnl_min: float | None = None, pnl_max: float | None = None,
             confidence_min: float | None = None, confidence_max: float | None = None,
             entry_after: str | None = None, entry_before: str | None = None,
             exit_after: str | None = None, exit_before: str | None = None) -> dict[str, Any]:
    return dict(locals())


@router.get("/trades")
async def list_trades(principal: CurrentPrincipal, db: DB,
                      f: dict[str, Any] = Depends(_filters),
                      sort: Literal["exit_desc", "exit_asc", "pnl_desc", "pnl_asc"] = "exit_desc",
                      page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)
                      ) -> dict[str, Any]:
    rows = await _trades(db, sort=sort, **f)
    start = (page - 1) * page_size
    return {"total": len(rows), "page": page, "page_size": page_size,
            "items": [_position_json(p, None) for p in rows[start:start + page_size]]}


CSV_FIELDS = ["id", "session_date", "symbol", "strategy", "qty", "avg_entry", "avg_exit",
              "entry_time", "exit_time", "holding_seconds", "exit_reason", "net_pnl",
              "return_pct", "confidence"]


@router.get("/trades/export.csv")
async def export_trades(principal: CurrentPrincipal, db: DB,
                        f: dict[str, Any] = Depends(_filters)) -> StreamingResponse:
    rows = [_position_json(p, None) for p in await _trades(db, sort="exit_asc", **f)]

    def gen() -> Iterator[str]:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        yield buf.getvalue()

    return StreamingResponse(gen(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="trades-{_today()}.csv"'})


@router.get("/trades/{trade_id}")
async def trade_detail(trade_id: uuid.UUID, principal: CurrentPrincipal, db: DB
                       ) -> dict[str, Any]:
    """The full audit chain: signal → decision → (LLM) → risk → orders/fills → exit."""
    p = await trading_repo.get_position(db, trade_id)
    if p is None:
        raise Problem(404, "Trade not found")
    decision = await db.get(AiDecision, p.entry_decision_id) if p.entry_decision_id else None
    signal = await db.get(TradingSignal, decision.signal_id) if decision else None
    llm = await db.get(LlmCall, decision.llm_call_id) if decision and decision.llm_call_id \
        else None
    risk = (await db.execute(select(RiskEvaluation).where(
        RiskEvaluation.decision_id == decision.id))).scalar_one_or_none() if decision else None
    orders = await trading_repo.position_orders(db, p.id)
    ids = [o.id for o in orders]
    events = list((await db.execute(select(OrderEvent).where(OrderEvent.order_id.in_(ids))
                                    .order_by(OrderEvent.ts))).scalars()) if ids else []
    fills = list((await db.execute(select(Fill).where(Fill.order_id.in_(ids))
                                   .order_by(Fill.ts))).scalars()) if ids else []
    active = await settings_repo.get_active(db)
    feed = parse_config(active.config).data.feed if active else "iex"
    day_start = datetime.combine(p.session_date, time(9, 30), ET)
    bars = (await db.execute(select(Bar1m).where(
        Bar1m.symbol == p.symbol, Bar1m.feed == feed, Bar1m.ts >= day_start,
        Bar1m.ts < day_start + timedelta(hours=6, minutes=30)).order_by(Bar1m.ts))).scalars()
    from intraday.ai.explain import exit_explanation

    exit_text = exit_explanation(p.exit_reason or "", price=p.avg_exit_price,
                                 minutes=(p.holding_seconds or 0) // 60) if p.exit_reason else None
    return {
        "position": _position_json(p, None), "exit_detail": p.exit_detail,
        "entry": {
            "signal": None if signal is None else {
                "id": str(signal.id), "bar_ts": signal.bar_ts.isoformat(),
                "features": signal.features, "conditions": signal.conditions,
                "factor_scores": signal.factor_scores, "score": float(signal.score),
                "regime": signal.regime, "signals": signal.signals},
            "decision": None if decision is None else {
                "id": str(decision.id), "ts": decision.ts.isoformat(),
                "decision_result": decision.decision_result, "ai_mode": decision.ai_mode,
                "llm_verdict": decision.llm_verdict, "confidence": float(decision.confidence),
                "entry_ref_price": str(decision.entry_ref_price),
                "stop_loss": str(decision.stop_loss), "take_profit": str(decision.take_profit),
                "risk_reward": float(decision.risk_reward)},
            "llm_call": None if llm is None else {
                "id": str(llm.id), "purpose": llm.purpose, "model": llm.model,
                "valid": llm.valid, "parsed": llm.parsed, "latency_ms": llm.latency_ms,
                "error": llm.error},
            "risk_evaluation": None if risk is None else {
                "approved": risk.approved, "checks": risk.checks, "sizing": risk.sizing,
                "qty": risk.qty, "max_loss": str(risk.max_loss)}},
        "orders": [{**_order_json(o),
                    "events": [{"ts": e.ts.isoformat(), "source": e.source, "event": e.event}
                               for e in events if e.order_id == o.id],
                    "fills": [{"ts": f.ts.isoformat(), "qty": f.qty, "price": str(f.price)}
                              for f in fills if f.order_id == o.id]} for o in orders],
        "exit": {"reason": p.exit_reason, "detail": p.exit_detail},
        "explanation": {
            "entry_text": (decision.reasoning_llm or decision.reasoning) if decision else None,
            "exit_text": exit_text,
            "source": "LLM" if decision and decision.reasoning_llm else "TEMPLATE"},
        "bars": [{"t": b.ts.isoformat(), "o": float(b.open), "h": float(b.high),
                  "l": float(b.low), "c": float(b.close), "v": b.volume} for b in bars],
    }


# ------------------------------------------------------------------ calendar

@router.get("/calendar")
async def calendar_month(principal: CurrentPrincipal, db: DB,
                         month: str = Query(..., pattern=r"^\d{4}-\d{2}$")) -> dict[str, Any]:
    y, m = (int(x) for x in month.split("-"))
    first = date(y, m, 1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    rows = {r.session_date: r for r in (await db.execute(select(DailyPnl).where(
        DailyPnl.session_date.between(first, last)))).scalars()}
    cal = list((await db.execute(select(MarketCalendarDay.session_date).where(
        MarketCalendarDay.session_date.between(first, last)))).scalars())
    sessions = set(cal)
    holidays: set[date] = set()
    if sessions:   # only when the calendar covers the month: weekdays without a session
        d = first
        while d <= min(last, max(sessions)):
            if d.weekday() < 5 and d not in sessions:
                holidays.add(d)
            d += timedelta(days=1)
    return perf.month_days(month, holidays, rows)


@router.get("/calendar/{day}")
async def calendar_day(day: date, principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    row = await db.get(DailyPnl, day)
    trades = list((await db.execute(select(Position).where(
        Position.session_date == day, Position.status == "CLOSED")
        .order_by(Position.entry_time))).scalars())
    summary = None if row is None else {c.name: _plain(getattr(row, c.name))
                                         for c in DailyPnl.__table__.columns}
    return {"summary": summary, "trades": [
        {"id": str(p.id), "symbol": p.symbol, "side": "long", "qty": _qty(p),
         "entry_time": p.entry_time.isoformat() if p.entry_time else None,
         "exit_time": p.exit_time.isoformat() if p.exit_time else None,
         "net_pnl": str(p.net_pnl) if p.net_pnl is not None else None,
         "return_pct": float(p.return_pct) * 100 if p.return_pct is not None else None,
         "strategy": p.strategy, "exit_reason": p.exit_reason} for p in trades]}


def _plain(v: Any) -> Any:
    return str(v) if isinstance(v, (Decimal, date, datetime)) else v


def _qty(p: Position) -> int:
    return int((p.exit_detail or {}).get("exit_qty") or p.qty)


# ------------------------------------------------------------------ performance

async def _closed(db: DB, date_from: date | None, date_to: date | None) -> list[Any]:
    q = _trade_query(date_from, date_to, None, None, None, None, None, None, None, None)
    return [perf.trade_of(p) for p in (await db.execute(q.order_by(Position.exit_time)))
            .scalars()]


@router.get("/performance/summary")
async def performance_summary(principal: CurrentPrincipal, db: DB,
                              date_from: date | None = Query(None, alias="from"),
                              date_to: date | None = Query(None, alias="to")) -> dict[str, Any]:
    trades = await _closed(db, date_from, date_to)
    return perf.summary(trades, float(await _start_equity(db)))


@router.get("/performance/pnl")
async def performance_pnl(principal: CurrentPrincipal, db: DB,
                          period: Literal["day", "week", "month"] = "day",
                          date_from: date | None = Query(None, alias="from"),
                          date_to: date | None = Query(None, alias="to")) -> list[dict[str, Any]]:
    return perf.bucketed_pnl(await _closed(db, date_from, date_to), period)


@router.get("/performance/equity-curve")
async def equity_curve(principal: CurrentPrincipal, db: DB,
                       date_from: date | None = Query(None, alias="from"),
                       date_to: date | None = Query(None, alias="to")) -> list[dict[str, Any]]:
    start = await _start_equity(db)
    eq, out = start, []
    for b in perf.bucketed_pnl(await _closed(db, date_from, date_to), "day"):
        eq = start + Decimal(b["cumulative"])
        out.append({"date": b["period"], "equity": str(eq), "net_pnl": b["net_pnl"]})
    return out


@router.get("/performance/breakdown")
async def performance_breakdown(
        principal: CurrentPrincipal, db: DB,
        by: Literal["strategy", "symbol", "hour", "confidence_bucket", "exit_reason"] = "strategy",
        date_from: date | None = Query(None, alias="from"),
        date_to: date | None = Query(None, alias="to")) -> list[dict[str, Any]]:
    return perf.breakdown(await _closed(db, date_from, date_to), by)
