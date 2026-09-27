"""Calendar edge cases: early close, holidays, DST, entry windows (docs/design/08 §8, G1)."""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from intraday.core.runtime_config import HoursSettings
from intraday.marketdata.calendar import MarketCalendar, SessionDay, entry_window, et_datetime

# Alpaca /v2/calendar format: open/close in ET "HH:MM".
RAW = [
    {"date": "2026-03-06", "open": "09:30", "close": "16:00"},   # Fri, EST (UTC-5)
    {"date": "2026-03-09", "open": "09:30", "close": "16:00"},   # Mon after DST start (UTC-4)
    {"date": "2026-07-02", "open": "09:30", "close": "16:00"},   # Thu; Fri Jul 3 is a holiday
    {"date": "2026-07-06", "open": "09:30", "close": "16:00"},   # Mon
    {"date": "2026-10-30", "open": "09:30", "close": "16:00"},   # Fri, EDT
    {"date": "2026-11-02", "open": "09:30", "close": "16:00"},   # Mon after DST end (UTC-5)
    {"date": "2026-11-25", "open": "09:30", "close": "16:00"},   # Wed
    {"date": "2026-11-27", "open": "09:30", "close": "13:00"},   # Fri after Thanksgiving: half day
]
CAL = MarketCalendar(SessionDay.from_alpaca(r) for r in RAW)
HOURS = HoursSettings()


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_dst_changes_utc_open_time() -> None:
    assert CAL.session(date(2026, 3, 6)).open_at == utc(2026, 3, 6, 14, 30)   # EST
    assert CAL.session(date(2026, 3, 9)).open_at == utc(2026, 3, 9, 13, 30)   # EDT
    assert CAL.session(date(2026, 10, 30)).close_at == utc(2026, 10, 30, 20, 0)
    assert CAL.session(date(2026, 11, 2)).close_at == utc(2026, 11, 2, 21, 0)


def test_holiday_is_not_a_session() -> None:
    assert CAL.session(date(2026, 7, 3)) is None
    assert not CAL.is_open(utc(2026, 7, 3, 15, 0))
    assert CAL.current_or_next(utc(2026, 7, 3, 15, 0)).session_date == date(2026, 7, 6)
    assert CAL.previous(date(2026, 7, 6)).session_date == date(2026, 7, 2)


def test_is_open_boundaries() -> None:
    s = CAL.session(date(2026, 11, 25))
    assert not CAL.is_open(s.open_at - timedelta(seconds=1))
    assert CAL.is_open(s.open_at)
    assert not CAL.is_open(s.close_at)  # close is exclusive


def test_early_close_detected() -> None:
    assert CAL.session(date(2026, 11, 27)).early_close
    assert not CAL.session(date(2026, 11, 25)).early_close


def test_normal_day_window_uses_configured_times() -> None:
    w = entry_window(CAL.session(date(2026, 11, 25)), HOURS)
    assert w.trading_start == et_datetime(date(2026, 11, 25), time(9, 35))
    assert w.no_new_trades_after == et_datetime(date(2026, 11, 25), time(15, 30))
    assert w.force_close == et_datetime(date(2026, 11, 25), time(15, 55))
    assert w.trading_end == et_datetime(date(2026, 11, 25), time(16, 0))


def test_half_day_window_is_offset_from_actual_close() -> None:
    w = entry_window(CAL.session(date(2026, 11, 27)), HOURS)
    et = date(2026, 11, 27)
    assert w.trading_start == et_datetime(et, time(9, 35))
    assert w.no_new_trades_after == et_datetime(et, time(12, 30))  # close 13:00 - 30 min
    assert w.force_close == et_datetime(et, time(12, 55))          # close - 5 min
    assert w.trading_end == et_datetime(et, time(13, 0))
    # A fixed 15:55 force-close would be long after the market closed.
    assert w.force_close < CAL.session(et).close_at


@pytest.mark.parametrize(("now", "open_"), [
    (utc(2026, 11, 25, 14, 34), False),   # 09:34 ET
    (utc(2026, 11, 25, 14, 35), True),    # 09:35 ET
    (utc(2026, 11, 25, 20, 29), True),    # 15:29 ET
    (utc(2026, 11, 25, 20, 30), False),   # 15:30 ET
])
def test_entry_window_boundaries(now: datetime, open_: bool) -> None:
    assert entry_window(CAL.session(date(2026, 11, 25)), HOURS).entries_open(now) is open_


def test_last_n_sessions_skips_holidays_and_unopened_days() -> None:
    days = CAL.last_n_sessions(2, utc(2026, 7, 6, 12, 0))  # before Monday's open
    assert [d.session_date for d in days] == [date(2026, 3, 9), date(2026, 7, 2)][-2:] or \
        [d.session_date for d in days] == [date(2026, 3, 9), date(2026, 7, 2)]
    days = CAL.last_n_sessions(2, utc(2026, 7, 6, 14, 0))  # after Monday's open
    assert [d.session_date for d in days] == [date(2026, 7, 2), date(2026, 7, 6)]


def test_compact_time_format_accepted() -> None:
    s = SessionDay.from_alpaca({"date": "2026-11-27", "open": "0930", "close": "1300"})
    assert s.early_close and s.close_at == utc(2026, 11, 27, 18, 0)


def test_status_payload() -> None:
    st = CAL.status(utc(2026, 11, 27, 15, 0))
    assert st["is_open"] is True and st["early_close"] is True
    assert st["session_date"] == "2026-11-27" and st["calendar_until"] == "2026-11-27"
