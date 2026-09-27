"""Backtest metrics (SPEC §24). Pure functions over trades and an equity curve."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

TRADING_DAYS = 252


@dataclass(frozen=True)
class TradeResult:
    symbol: str
    strategy: str
    session_date: date
    entry_time: datetime
    exit_time: datetime
    qty: int
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    exit_reason: str
    score: float

    @property
    def holding_seconds(self) -> int:
        return int((self.exit_time - self.entry_time).total_seconds())

    @property
    def return_pct(self) -> Decimal:
        cost = self.entry_price * self.qty
        return self.net_pnl / cost * 100 if cost else Decimal(0)


def max_drawdown(equity: Sequence[float]) -> tuple[float, float]:
    """(max drawdown in currency, as a fraction of the peak)."""
    peak, worst, worst_pct = -math.inf, 0.0, 0.0
    for e in equity:
        peak = max(peak, e)
        dd = peak - e
        if dd > worst:
            worst = dd
            worst_pct = dd / peak if peak > 0 else 0.0
    return worst, worst_pct


def sharpe(daily_returns: Sequence[float]) -> float | None:
    if len(daily_returns) < 2:
        return None
    m = sum(daily_returns) / len(daily_returns)
    sd = math.sqrt(sum((r - m) ** 2 for r in daily_returns) / (len(daily_returns) - 1))
    return m / sd * math.sqrt(TRADING_DAYS) if sd > 0 else None


def sortino(daily_returns: Sequence[float]) -> float | None:
    if len(daily_returns) < 2:
        return None
    m = sum(daily_returns) / len(daily_returns)
    downside = [min(0.0, r) ** 2 for r in daily_returns]
    dd = math.sqrt(sum(downside) / len(daily_returns))
    return m / dd * math.sqrt(TRADING_DAYS) if dd > 0 else None


def streaks(pnls: Sequence[Decimal]) -> tuple[int, int]:
    best = worst = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif p < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        best, worst = max(best, cur_w), max(worst, cur_l)
    return best, worst


def summarize(trades: Sequence[TradeResult], daily_equity: Sequence[tuple[date, float]],
              starting_capital: float, curve: Sequence[float] | None = None) -> dict[str, Any]:
    """All SPEC §24 metrics. ``daily_equity`` = end-of-session equity per session."""
    final = daily_equity[-1][1] if daily_equity else starting_capital
    total_return = final / starting_capital - 1 if starting_capital else 0.0
    days = len(daily_equity)
    annualized = ((1 + total_return) ** (TRADING_DAYS / days) - 1) if days and \
        total_return > -1 else None
    prev, rets = starting_capital, []
    for _, eq in daily_equity:
        rets.append(eq / prev - 1 if prev else 0.0)
        prev = eq
    dd, dd_pct = max_drawdown([starting_capital, *(curve or [e for _, e in daily_equity])])
    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins, Decimal(0)), -sum(losses, Decimal(0))
    best_streak, worst_streak = streaks(pnls)

    def money(x: Decimal | None) -> float | None:
        return None if x is None else float(round(x, 2))

    by_strategy: dict[str, dict[str, Any]] = {}
    for t in trades:
        s = by_strategy.setdefault(t.strategy, {"trades": 0, "wins": 0, "net_pnl": Decimal(0)})
        s["trades"] += 1
        s["wins"] += 1 if t.net_pnl > 0 else 0
        s["net_pnl"] += t.net_pnl
    return {
        "sessions": days,
        "starting_capital": round(starting_capital, 2),
        "final_equity": round(final, 2),
        "total_return_pct": round(total_return * 100, 4),
        "annualized_return_pct": None if annualized is None else round(annualized * 100, 4),
        "max_drawdown": round(dd, 2),
        "max_drawdown_pct": round(dd_pct * 100, 4),
        "sharpe": None if (sh := sharpe(rets)) is None else round(sh, 4),
        "sortino": None if (so := sortino(rets)) is None else round(so, 4),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else None,
        "profit_factor": round(float(gross_win / gross_loss), 4) if gross_loss else None,
        "net_pnl": money(sum(pnls, Decimal(0))),
        "fees": money(sum((t.fees for t in trades), Decimal(0))),
        "average_trade": money(sum(pnls, Decimal(0)) / len(pnls)) if pnls else None,
        "average_win": money(gross_win / len(wins)) if wins else None,
        "average_loss": money(-gross_loss / len(losses)) if losses else None,
        "largest_win": money(max(wins)) if wins else None,
        "largest_loss": money(min(losses)) if losses else None,
        "largest_winning_streak": best_streak,
        "largest_losing_streak": worst_streak,
        "average_holding_minutes": round(sum(t.holding_seconds for t in trades)
                                         / len(trades) / 60, 2) if trades else None,
        "by_strategy": {k: {**v, "net_pnl": money(v["net_pnl"])}
                        for k, v in sorted(by_strategy.items())},
        "exit_reasons": _count(t.exit_reason for t in trades),
    }


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items()))
