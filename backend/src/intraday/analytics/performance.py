"""Live-trading P&L analytics (SPEC §24; docs/design/05 §5, §8). Pure.

Closed positions are turned into the same ``TradeResult`` the backtester uses,
so live and backtest metrics come from one implementation.
"""

from __future__ import annotations

import calendar as _cal
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from intraday.backtest.metrics import TradeResult, summarize
from intraday.core.timeutil import ET

ZERO = Decimal(0)


def trade_of(p: Any) -> TradeResult:
    d = p.exit_detail or {}
    qty = int(d.get("exit_qty") or 0) or p.qty
    return TradeResult(
        symbol=p.symbol, strategy=p.strategy or "unknown", session_date=p.session_date,
        entry_time=p.entry_time or p.created_at, exit_time=p.exit_time or p.updated_at,
        qty=qty, entry_price=Decimal(p.avg_entry_price or 0),
        exit_price=Decimal(p.avg_exit_price or 0), gross_pnl=Decimal(p.gross_pnl or 0),
        fees=Decimal(p.fees or 0), net_pnl=Decimal(p.net_pnl or 0),
        exit_reason=p.exit_reason or "UNKNOWN", score=float(p.score or 0))


def summary(trades: Sequence[TradeResult], start_equity: float,
            daily_equity: Sequence[tuple[date, float]] | None = None) -> dict[str, Any]:
    if not daily_equity:
        eq, acc = start_equity, []
        by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
        for t in trades:
            by_day[t.session_date] += t.net_pnl
        for day in sorted(by_day):
            eq += float(by_day[day])
            acc.append((day, eq))
        daily_equity = acc
    out = summarize(trades, daily_equity, start_equity)
    out["cumulative_pnl"] = out.pop("net_pnl")
    return out


def bucket_key(d: date, period: str) -> str:
    if period == "week":
        monday = d - timedelta(days=d.weekday())
        return monday.isoformat()
    if period == "month":
        return f"{d:%Y-%m}"
    return d.isoformat()


def bucketed_pnl(trades: Iterable[TradeResult], period: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for t in sorted(trades, key=lambda t: t.exit_time):
        k = bucket_key(t.session_date, period)
        b = buckets.setdefault(k, {"period": k, "net_pnl": ZERO, "trades": 0, "wins": 0})
        b["net_pnl"] += t.net_pnl
        b["trades"] += 1
        b["wins"] += t.net_pnl > 0
    cum = ZERO
    out = []
    for k in sorted(buckets):
        b = buckets[k]
        cum += b["net_pnl"]
        out.append({**b, "net_pnl": str(b["net_pnl"]), "cumulative": str(cum)})
    return out


def _confidence_bucket(score: float) -> str:
    lo = min(int(score * 10), 9) / 10
    return f"{lo:.1f}-{lo + 0.1:.1f}"


def breakdown(trades: Iterable[TradeResult], by: str) -> list[dict[str, Any]]:
    keys: dict[str, Any] = {
        "strategy": lambda t: t.strategy, "symbol": lambda t: t.symbol,
        "hour": lambda t: f"{t.entry_time.astimezone(ET):%H}:00",
        "confidence_bucket": lambda t: _confidence_bucket(t.score),
        "exit_reason": lambda t: t.exit_reason,
    }
    keyf = keys[by]
    groups: dict[str, list[TradeResult]] = defaultdict(list)
    for t in trades:
        groups[keyf(t)].append(t)
    out = []
    for k in sorted(groups):
        ts = groups[k]
        pnls = [t.net_pnl for t in ts]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        out.append({"key": k, "trades": len(ts), "wins": len(wins),
                    "win_rate_pct": round(len(wins) / len(ts) * 100, 2),
                    "net_pnl": str(sum(pnls, ZERO)),
                    "avg_pnl": str((sum(pnls, ZERO) / len(ts)).quantize(Decimal("0.01"))),
                    "profit_factor": round(float(sum(wins, ZERO) / -sum(losses, ZERO)), 4)
                    if losses else None})
    return out


def strategy_stats(trades: Sequence[TradeResult]) -> dict[str, dict[str, Any]]:
    """strategy_performance rows for one session."""
    out: dict[str, dict[str, Any]] = {}
    for strat in sorted({t.strategy for t in trades}):
        ts = [t for t in trades if t.strategy == strat]
        wins = [t.net_pnl for t in ts if t.net_pnl > 0]
        losses = [t.net_pnl for t in ts if t.net_pnl <= 0]
        out[strat] = {
            "trades": len(ts), "wins": len(wins), "losses": len(losses),
            "net_pnl": sum((t.net_pnl for t in ts), ZERO),
            "avg_win": (sum(wins, ZERO) / len(wins)).quantize(Decimal("0.01")) if wins else None,
            "avg_loss": (sum(losses, ZERO) / len(losses)).quantize(Decimal("0.01"))
            if losses else None,
            "profit_factor": (sum(wins, ZERO) / -sum(losses, ZERO)).quantize(Decimal("0.0001"))
            if losses and sum(losses, ZERO) < 0 else None,
            "avg_holding_seconds": int(sum(t.holding_seconds for t in ts) / len(ts)),
        }
    return out


def month_days(month: str, holidays: set[date], rows: dict[date, Any]) -> dict[str, Any]:
    """Calendar cells: PROFIT / LOSS / FLAT / NO_TRADING / HOLIDAY (weekends omitted)."""
    year, mon = (int(x) for x in month.split("-"))
    days: list[dict[str, Any]] = []
    totals: dict[str, Any] = {"realized_net": ZERO, "trades": 0, "wins": 0, "losses": 0}
    for day in range(1, _cal.monthrange(year, mon)[1] + 1):
        d = date(year, mon, day)
        if d.weekday() >= 5:
            continue
        r = rows.get(d)
        if r is not None and r.trades:
            net = Decimal(r.realized_net)
            status = "PROFIT" if net > 0 else "LOSS" if net < 0 else "FLAT"
            totals["realized_net"] += net
            totals["trades"] += r.trades
            totals["wins"] += r.wins
            totals["losses"] += r.losses
            days.append({"date": d.isoformat(), "status": status, "realized_net": str(net),
                         "trades": r.trades, "wins": r.wins, "losses": r.losses,
                         "win_rate": float(r.win_rate) if r.win_rate is not None else None,
                         "largest_win": _s(r.largest_win), "largest_loss": _s(r.largest_loss),
                         "shares_traded": r.shares_traded,
                         "notional_traded": str(r.notional_traded),
                         "daily_loss_limit_hit": r.daily_loss_limit_hit})
        else:
            status = "HOLIDAY" if d in holidays else "NO_TRADING"
            days.append({"date": d.isoformat(), "status": status, "realized_net": "0",
                         "trades": 0, "wins": 0, "losses": 0})
    totals["realized_net"] = str(totals["realized_net"])
    return {"month": month, "days": days, "totals": totals}


def _s(x: Any) -> str | None:
    return None if x is None else str(x)
