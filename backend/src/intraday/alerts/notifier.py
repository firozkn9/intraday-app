"""Alert delivery (SPEC §26; docs/design/05 §9).

Every alert is stored in ``alerts`` (the dashboard channel). The engine's
alert loop sends unacknowledged alerts at or above
``alerts.email_min_severity`` by email once each (``channels_sent``). SMTP runs
in a worker thread so the event loop never blocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.core.logging import get_logger
from intraday.db.models import Alert
from intraday.db.session import transaction

log = get_logger(__name__)

SEVERITY_ORDER = {"INFO": 0, "WARN": 1, "HIGH": 2, "CRITICAL": 3}


class Notifier(Protocol):
    channel: str

    async def send(self, alert: Alert) -> None: ...


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str | None
    password: str | None
    sender: str
    to: list[str]


class EmailNotifier:
    channel = "email"

    def __init__(self, cfg: SmtpConfig) -> None:
        self.cfg = cfg

    def _send_sync(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=15) as smtp:
            if self.cfg.port != 25:
                with contextlib.suppress(smtplib.SMTPNotSupportedError):   # test catchers
                    smtp.starttls()
            if self.cfg.user and self.cfg.password:
                smtp.login(self.cfg.user, self.cfg.password)
            smtp.send_message(msg)

    async def send(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[Intraday PAPER] {alert.severity}: {alert.type}"
        msg["From"] = self.cfg.sender
        msg["To"] = ", ".join(self.cfg.to)
        msg.set_content(f"{alert.message}\n\nSeverity: {alert.severity}\nType: {alert.type}\n"
                        f"First seen: {alert.ts.isoformat()}\nCount: {alert.count}\n\n"
                        "Paper trading only. Review and acknowledge in the dashboard.")
        await asyncio.to_thread(self._send_sync, msg)


async def dispatch(sm: async_sessionmaker[AsyncSession], notifiers: list[Notifier], *,
                   min_severity: str, enabled: bool = True) -> int:
    """Send pending alerts once per channel. Returns the number sent."""
    if not enabled or not notifiers:
        return 0
    floor = SEVERITY_ORDER[min_severity]
    async with sm() as s:
        pending = [a for a in (await s.execute(
            select(Alert).where(Alert.acknowledged_at.is_(None)).order_by(Alert.ts)
            .limit(50))).scalars() if SEVERITY_ORDER.get(a.severity, 0) >= floor]
    sent = 0
    for n in notifiers:
        for a in pending:
            if n.channel in (a.channels_sent or []):
                continue
            try:
                await n.send(a)
            except (OSError, smtplib.SMTPException) as exc:
                log.warning("alert_send_failed", channel=n.channel, alert=str(a.id),
                            error=str(exc))
                continue
            async with transaction(sm) as s:
                row = await s.get(Alert, a.id, with_for_update=True)
                if row is not None:
                    row.channels_sent = [*(row.channels_sent or []), n.channel]
            sent += 1
    return sent
