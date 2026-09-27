"""Trading calendar and entry-window arithmetic (docs/design/08 §8, decision G1).

Session days come from Alpaca's /v2/calendar (regular open/close in ET, early
closes included). On early-close days the configured cutoffs become offsets
from the actual close.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from intraday.core.hard_limits import REGULAR_MARKET_CLOSE
from intraday.core.runtime_config import HoursSettings
from intraday.core.timeutil import ET


@dataclass(frozen=True)
class SessionDay:
    session_date: date
    open_at: datetime  # UTC
    close_at: datetime  # UTC

    @property
    def early_close(self) -> bool:
        return self.close_at.astimezone(ET).time() < REGULAR_MARKET_CLOSE

    @classmethod
    def from_alpaca(cls, raw: dict[str, Any]) -> SessionDay:
        d = date.fromisoformat(str(raw["date"]))
        return cls(d, et_datetime(d, _hhmm(str(raw["open"]))),
                   et_datetime(d, _hhmm(str(raw["close"]))))

    def to_row(self) -> dict[str, Any]:
        return {"session_date": self.session_date, "open_at": self.open_at,
                "close_at": self.close_at, "early_close": self.early_close}


def _hhmm(text: str) -> time:
    text = text.strip()
    if ":" not in text and len(text) == 4:  # "0930" style
        text = f"{text[:2]}:{text[2:]}"
    return time.fromisoformat(text)


def et_datetime(d: date, t: time) -> datetime:
    """Wall-clock ET time on ``d`` → UTC. zoneinfo handles DST per date."""
    return datetime.combine(d, t, tzinfo=ET).astimezone(UTC)


@dataclass(frozen=True)
class EntryWindow:
    session: SessionDay
    trading_start: datetime
    no_new_trades_after: datetime
    force_close: datetime
    trading_end: datetime

    def entries_open(self, now: datetime) -> bool:
        return self.trading_start <= now < self.no_new_trades_after

    def to_json(self) -> dict[str, Any]:
        return {k: getattr(self, k).isoformat()
                for k in ("trading_start", "no_new_trades_after", "force_close", "trading_end")}


def entry_window(session: SessionDay, hours: HoursSettings) -> EntryWindow:
    d = session.session_date
    start = max(et_datetime(d, hours.trading_start), session.open_at)
    if session.early_close:
        close = session.close_at
        no_new = close - timedelta(minutes=hours.early_close_no_new_trades_offset_min)
        force = close - timedelta(minutes=hours.early_close_force_close_offset_min)
        end = close
    else:
        no_new = et_datetime(d, hours.no_new_trades_after)
        force = et_datetime(d, hours.force_close)
        end = et_datetime(d, hours.trading_end)
    no_new = max(no_new, start)  # a very short session can leave no entry window
    return EntryWindow(session, start, no_new, force, end)


class MarketCalendar:
    """Immutable, sorted set of session days."""

    def __init__(self, days: Iterable[SessionDay]) -> None:
        self._days = sorted(days, key=lambda s: s.session_date)
        self._dates = [s.session_date for s in self._days]
        self._by_date = {s.session_date: s for s in self._days}

    def __len__(self) -> int:
        return len(self._days)

    @property
    def first(self) -> date | None:
        return self._dates[0] if self._dates else None

    @property
    def last(self) -> date | None:
        return self._dates[-1] if self._dates else None

    def covers(self, d: date) -> bool:
        return self.first is not None and self.first <= d <= self.last  # type: ignore[operator]

    def session(self, d: date) -> SessionDay | None:
        return self._by_date.get(d)

    def is_open(self, now: datetime) -> bool:
        s = self.session(now.astimezone(ET).date())
        return s is not None and s.open_at <= now < s.close_at

    def current_or_next(self, now: datetime) -> SessionDay | None:
        """The session in progress, or the next one to open."""
        i = bisect.bisect_left(self._dates, now.astimezone(ET).date())
        for s in self._days[i:]:
            if now < s.close_at:
                return s
        return None

    def previous(self, d: date) -> SessionDay | None:
        i = bisect.bisect_left(self._dates, d)
        return self._days[i - 1] if i > 0 else None

    def sessions_between(self, start: date, end: date) -> list[SessionDay]:
        lo = bisect.bisect_left(self._dates, start)
        hi = bisect.bisect_right(self._dates, end)
        return self._days[lo:hi]

    def last_n_sessions(self, n: int, until: datetime) -> list[SessionDay]:
        """The n most recent sessions that have opened by ``until``."""
        i = bisect.bisect_right(self._dates, until.astimezone(ET).date())
        opened = [s for s in self._days[:i] if s.open_at <= until]
        return opened[-n:]

    def status(self, now: datetime) -> dict[str, Any]:
        s = self.current_or_next(now)
        return {
            "is_open": self.is_open(now),
            "session_date": s.session_date.isoformat() if s else None,
            "open_at": s.open_at.isoformat() if s else None,
            "close_at": s.close_at.isoformat() if s else None,
            "early_close": s.early_close if s else None,
            "calendar_until": self.last.isoformat() if self.last else None,
        }
