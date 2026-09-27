"""Versioned runtime settings (settings_versions)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from intraday.core.runtime_config import TradingConfig
from intraday.db.models import AccountSnapshot, SettingsVersion

INITIAL_COMMENT = "Initial defaults from config/defaults.yaml"


class StaleSettingsVersion(Exception):
    def __init__(self, active_version: int) -> None:
        self.active_version = active_version
        super().__init__(f"settings were changed meanwhile (active version {active_version})")


async def get_active(session: AsyncSession) -> SettingsVersion | None:
    res = await session.execute(select(SettingsVersion).where(SettingsVersion.is_active))
    return res.scalar_one_or_none()


async def get_version(session: AsyncSession, version: int) -> SettingsVersion | None:
    return await session.get(SettingsVersion, version)


async def ensure_initial(session: AsyncSession, defaults: TradingConfig) -> SettingsVersion:
    """Insert version 1 from defaults if no version is active. Safe to race."""
    existing = await get_active(session)
    if existing is not None:
        # Checked first because ON CONFLICT still consumes a sequence value,
        # which would leave gaps in the version numbers.
        return existing
    stmt = (
        insert(SettingsVersion)
        .values(config=defaults.to_json(), comment=INITIAL_COMMENT, is_active=True)
        .on_conflict_do_nothing(index_elements=["is_active"],
                                index_where=text("is_active"))  # must match the index
    )
    await session.execute(stmt)
    active = await get_active(session)
    if active is None:  # pragma: no cover - impossible after the insert above
        raise RuntimeError("no active settings version after initialisation")
    return active


async def activate_new(
    session: AsyncSession,
    *,
    config: dict[str, Any],
    base_version: int,
    user_id: uuid.UUID | None,
    comment: str,
) -> SettingsVersion:
    """Insert a new active version; raise StaleSettingsVersion if base isn't active."""
    res = await session.execute(
        select(SettingsVersion).where(SettingsVersion.is_active).with_for_update()
    )
    active = res.scalar_one_or_none()
    if active is None or active.version != base_version:
        raise StaleSettingsVersion(active.version if active else 0)
    await session.execute(
        update(SettingsVersion).where(SettingsVersion.version == active.version)
        .values(is_active=False)
    )
    new = SettingsVersion(config=config, created_by=user_id, comment=comment, is_active=True)
    session.add(new)
    await session.flush()
    return new


async def history(session: AsyncSession, limit: int = 50) -> list[SettingsVersion]:
    res = await session.execute(
        select(SettingsVersion).order_by(SettingsVersion.version.desc()).limit(limit + 1)
    )
    return list(res.scalars())


async def reference_equity(session: AsyncSession, active: TradingConfig,
                           proposed: TradingConfig) -> Decimal:
    """Equity used for hard-limit checks.

    Latest broker account snapshot if one exists; otherwise the *smaller* of the
    active and proposed paper balances, so raising the balance can't be used to
    lift hard limits in the same edit.
    """
    res = await session.execute(
        select(AccountSnapshot.equity).where(AccountSnapshot.source == "BROKER")
        .order_by(AccountSnapshot.ts.desc()).limit(1)
    )
    equity = res.scalar_one_or_none()
    if equity is not None:
        return Decimal(equity)
    return min(active.risk.paper_initial_balance, proposed.risk.paper_initial_balance)
