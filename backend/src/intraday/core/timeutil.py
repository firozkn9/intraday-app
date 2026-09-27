"""Time helpers. Storage is always UTC; trading sessions are US/Eastern."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(UTC)


def et_now() -> datetime:
    return datetime.now(ET)


def session_date(ts: datetime | None = None) -> date:
    """ET calendar date of ``ts`` (defaults to now)."""
    return (ts or utcnow()).astimezone(ET).date()
