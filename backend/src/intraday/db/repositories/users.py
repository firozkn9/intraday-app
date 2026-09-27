"""Users, sessions, and the audit log."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from intraday.db.models import AuditLog, Session, User


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    res = await session.execute(select(User).where(func.lower(User.email) == email.lower()))
    return res.scalar_one_or_none()


async def count_users(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(User))).scalar_one())


async def create_user(session: AsyncSession, email: str, password_hash: str,
                      role: str = "admin") -> User:
    user = User(email=email.lower(), password_hash=password_hash, role=role)
    session.add(user)
    await session.flush()
    return user


async def create_session(session: AsyncSession, *, id_hash: str, user_id: uuid.UUID,
                         csrf_token_hash: str, now: datetime, expires_at: datetime,
                         ip: str | None, user_agent: str | None) -> Session:
    row = Session(id_hash=id_hash, user_id=user_id, csrf_token_hash=csrf_token_hash,
                  created_at=now, expires_at=expires_at, last_seen_at=now, ip=ip,
                  user_agent=(user_agent or "")[:512] or None)
    session.add(row)
    await session.flush()
    return row


async def get_session(session: AsyncSession, id_hash: str) -> Session | None:
    return await session.get(Session, id_hash)


async def revoke_session(session: AsyncSession, id_hash: str, now: datetime) -> None:
    await session.execute(update(Session).where(Session.id_hash == id_hash)
                          .values(revoked_at=now))


async def revoke_other_sessions(session: AsyncSession, user_id: uuid.UUID, keep_id_hash: str,
                                now: datetime) -> None:
    await session.execute(
        update(Session)
        .where(Session.user_id == user_id, Session.id_hash != keep_id_hash,
               Session.revoked_at.is_(None))
        .values(revoked_at=now)
    )


async def touch_session(session: AsyncSession, id_hash: str, now: datetime,
                        min_interval: timedelta = timedelta(minutes=1)) -> None:
    await session.execute(
        update(Session)
        .where(Session.id_hash == id_hash, Session.last_seen_at < now - min_interval)
        .values(last_seen_at=now)
    )


async def audit(session: AsyncSession, *, action: str, user_id: uuid.UUID | None = None,
                target: str | None = None, before: dict[str, Any] | None = None,
                after: dict[str, Any] | None = None, ip: str | None = None,
                user_agent: str | None = None, result: str = "OK") -> None:
    session.add(AuditLog(action=action, user_id=user_id, target=target, before=before,
                         after=after, ip=ip, user_agent=(user_agent or "")[:512] or None,
                         result=result))
    await session.flush()
