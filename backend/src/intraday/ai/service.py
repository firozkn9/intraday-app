"""AI service: VETO, EXPLAIN and news sentiment around a provider (docs/design/09 §7).

- VETO: one review per proposal, before the risk engine. A failure counts as
  REJECT (verdict ERROR); ``max_consecutive_failures`` in a row sets
  ``unavailable`` (the engine then blocks entries with AI_UNAVAILABLE).
- EXPLAIN: rephrase the template; the number guard decides whether the LLM
  text is kept. Never changes a decision.
- Every call is recorded in ``llm_calls``.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intraday.ai import guard
from intraday.ai.providers import (
    FEATURE_WHITELIST,
    AIProvider,
    CallRecord,
    ExplainRequest,
    NewsHeadline,
    NewsRequest,
    ProviderError,
    VetoRequest,
)
from intraday.core.ids import uuid7
from intraday.core.logging import get_logger
from intraday.db.models import AiDecision, LlmCall, NewsItem
from intraday.db.session import transaction

log = get_logger(__name__)


def veto_request(rec: Any, news: Sequence[Any] = ()) -> VetoRequest:
    """Whitelisted, minimal request from a signal record (no account data at all)."""
    p = rec.proposal
    entry = float(p.entry_ref_price)
    f = rec.features
    return VetoRequest(
        symbol=rec.symbol, strategy=rec.strategy, signals=list(p.signals),
        features={k: (float(f[k]) if f.get(k) is not None else None)
                  for k in FEATURE_WHITELIST if k in f},
        factor_scores=dict(rec.factor_scores), score=float(rec.score),
        stop_distance_pct=round((entry - float(p.stop_loss)) / entry * 100, 3),
        target_distance_pct=round((float(p.take_profit) - entry) / entry * 100, 3),
        risk_reward=float(p.risk_reward_ratio), regime=dict(p.market_context),
        news=[NewsHeadline(headline=n.headline, tags=list(n.event_tags),
                           published_at=n.published_at.isoformat()) for n in news])


def explain_facts(rec: Any) -> dict[str, float | str | None]:
    p = rec.proposal
    facts: dict[str, float | str | None] = {
        k: (float(v) if isinstance(v, (int, float, Decimal)) else None)
        for k, v in rec.features.items() if k in FEATURE_WHITELIST}
    facts.update({"stop": float(p.stop_loss), "target": float(p.take_profit),
                  "risk_reward": float(p.risk_reward_ratio), "confidence": float(p.confidence),
                  "symbol": rec.symbol, "strategy": rec.strategy})
    return facts


class AIService:
    def __init__(self, sm: async_sessionmaker[AsyncSession], provider: AIProvider, *,
                 max_consecutive_failures: int = 3) -> None:
        self.sm = sm
        self.provider = provider
        self.max_consecutive_failures = max_consecutive_failures
        self.consecutive_failures = 0
        self.last_error: str | None = None

    @property
    def unavailable(self) -> bool:
        return self.consecutive_failures >= self.max_consecutive_failures

    async def _record(self, rec: CallRecord) -> Any:
        cid = uuid7()
        async with transaction(self.sm) as s:
            s.add(LlmCall(id=cid, purpose=rec.purpose, provider=rec.provider, model=rec.model,
                          request=rec.request, response_raw=rec.response_raw, parsed=rec.parsed,
                          valid=rec.valid, stop_reason=rec.stop_reason,
                          latency_ms=rec.latency_ms, input_tokens=rec.input_tokens,
                          output_tokens=rec.output_tokens, error=rec.error))
        return cid

    async def veto(self, rec: Any, news: Sequence[Any] = ()) -> tuple[str, Any]:
        """(APPROVE | REJECT | ERROR, llm_call id)."""
        try:
            result, call = await self.provider.review_trade(veto_request(rec, news))
        except ProviderError as exc:
            self.consecutive_failures += 1
            self.last_error = str(exc)
            log.warning("ai_veto_failed", symbol=rec.symbol, error=str(exc),
                        consecutive=self.consecutive_failures)
            return "ERROR", await self._record(exc.record)
        self.consecutive_failures, self.last_error = 0, None
        return result.verdict, await self._record(call)

    async def explain(self, decision_id: Any, rec: Any, template: str) -> str | None:
        """Rephrase; store reasoning_llm only if the number guard passes."""
        facts = explain_facts(rec)
        try:
            result, call = await self.provider.rephrase(ExplainRequest(template=template,
                                                                       facts=facts))
        except ProviderError as exc:
            await self._record(exc.record)
            return None
        problems = guard.check(result.text, template, facts)
        if problems:
            call.valid, call.error = False, "number guard: " + "; ".join(problems)[:400]
        await self._record(call)
        if problems:
            return None
        async with transaction(self.sm) as s:
            await s.execute(update(AiDecision).where(AiDecision.id == decision_id)
                            .values(reasoning_llm=result.text))
        return result.text

    async def classify(self, item: NewsItem) -> str:
        symbol = item.symbols[0] if item.symbols else ""
        try:
            result, call = await self.provider.classify_news(
                NewsRequest(symbol=symbol, headline=item.headline, summary=item.summary))
        except ProviderError as exc:
            await self._record(exc.record)
            return "UNKNOWN"
        await self._record(call)
        async with transaction(self.sm) as s:
            await s.execute(update(NewsItem).where(NewsItem.id == item.id)
                            .values(sentiment=result.sentiment, sentiment_source="LLM"))
        return result.sentiment
