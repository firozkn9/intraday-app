"""News ingestion (docs/design/09 §9): Alpaca News every 2 minutes for the watchlist.

Items are stored once (by Alpaca id) with deterministic event tags. Sentiment
is UNKNOWN unless the engine's AI service classifies it (VETO mode only).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.ai.news import event_tags
from intraday.core.logging import get_logger
from intraday.core.timeutil import utcnow
from intraday.db.models import NewsItem
from intraday.db.session import transaction
from intraday.marketdata.alpaca.clients import AlpacaDataClient
from intraday.marketdata.types import parse_ts

log = get_logger(__name__)

SYMBOLS_PER_CALL = 20


def news_row(raw: dict[str, Any]) -> dict[str, Any]:
    headline = str(raw.get("headline") or "")[:2000]
    summary = str(raw.get("summary") or "")[:4000] or None
    return {"source_id": str(raw["id"]), "symbols": [str(s) for s in raw.get("symbols") or []],
            "headline": headline, "summary": summary, "source": raw.get("source"),
            "url": raw.get("url"),
            "published_at": parse_ts(str(raw.get("created_at") or raw.get("updated_at"))),
            "event_tags": event_tags(headline, summary)}


async def store_news(sm: async_sessionmaker[AsyncSession], raws: Sequence[dict[str, Any]]
                     ) -> list[Any]:
    """Insert new items; returns the ids of rows actually inserted."""
    rows = [news_row(r) for r in raws if r.get("id") is not None]
    if not rows:
        return []
    async with transaction(sm) as s:
        res = await s.execute(insert(NewsItem).values(rows)
                              .on_conflict_do_nothing(index_elements=["source_id"])
                              .returning(NewsItem.id))
        return [r[0] for r in res.all()]


async def recent_news(s: AsyncSession, symbols: Sequence[str], since: datetime
                      ) -> list[NewsItem]:
    return list((await s.execute(
        select(NewsItem).where(NewsItem.symbols.overlap(list(symbols)),
                               NewsItem.published_at >= since)
        .order_by(NewsItem.published_at.desc()))).scalars())


class NewsService:
    def __init__(self, sm: async_sessionmaker[AsyncSession], data: AlpacaDataClient, *,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.sm = sm
        self.data = data
        self.clock = clock
        self.last_poll: datetime | None = None

    async def poll(self, symbols: Sequence[str], lookback_hours: int) -> list[Any]:
        now = self.clock()
        start = (self.last_poll - timedelta(minutes=5)) if self.last_poll else \
            now - timedelta(hours=lookback_hours)
        inserted: list[Any] = []
        for i in range(0, len(symbols), SYMBOLS_PER_CALL):
            raws = await self.data.get_news(symbols[i:i + SYMBOLS_PER_CALL], start)
            inserted += await store_news(self.sm, raws)
        self.last_poll = now
        if inserted:
            log.info("news_stored", count=len(inserted))
        return inserted
