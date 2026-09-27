"""LLM providers (docs/design/09 §7.2).

``AnthropicProvider`` calls the Messages API over httpx (same approach as the
Alpaca clients: small, fully mockable with respx). The model comes from
settings, never hardcoded. Responses are JSON validated locally against the
Pydantic schemas below; ``stop_reason`` ``refusal``/``max_tokens``, malformed
JSON, timeouts and HTTP errors are all failures (never an approval).

Data minimisation (CLAUDE.md): requests are built only from the whitelisted
DTOs here. There are no fields for balances, positions, P&L, account IDs,
order IDs or credentials, so they can't be sent.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
PROMPTS = Path(__file__).parent / "prompts"


# ------------------------------------------------------------------ DTOs (whitelist)

class NewsHeadline(BaseModel):
    headline: str
    tags: list[str] = []
    published_at: str


class VetoRequest(BaseModel):
    symbol: str
    strategy: str
    signals: list[str]
    features: dict[str, float | None]
    factor_scores: dict[str, float]
    score: float
    stop_distance_pct: float
    target_distance_pct: float
    risk_reward: float
    regime: dict[str, Any]
    news: list[NewsHeadline] = []


class VetoResult(BaseModel):
    verdict: Literal["APPROVE", "REJECT"]
    reasons: list[str] = Field(default_factory=list, max_length=5)
    contradictory_signals: list[str] = []
    unusual_conditions: list[str] = []


class ExplainRequest(BaseModel):
    template: str
    facts: dict[str, float | str | None]


class ExplainResult(BaseModel):
    text: str = Field(max_length=2000)


class NewsRequest(BaseModel):
    symbol: str
    headline: str
    summary: str | None = None


class NewsResult(BaseModel):
    sentiment: Literal["POSITIVE", "NEGATIVE", "NEUTRAL", "UNKNOWN"]


FEATURE_WHITELIST = ("price", "vwap_dist_pct", "rvol", "rsi", "ema9", "ema20", "macd_hist",
                     "atr_pct", "gap_pct", "day_change_pct", "range_pct", "adx")


@dataclass
class CallRecord:
    """Everything recorded in llm_calls for one call."""

    purpose: str
    provider: str
    model: str
    request: dict[str, Any]
    response_raw: str | None = None
    parsed: dict[str, Any] | None = None
    valid: bool = False
    stop_reason: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


class ProviderError(Exception):
    def __init__(self, message: str, record: CallRecord) -> None:
        super().__init__(message)
        self.record = record


class AIProvider(Protocol):
    name: str
    model: str

    async def review_trade(self, req: VetoRequest) -> tuple[VetoResult, CallRecord]: ...
    async def rephrase(self, req: ExplainRequest) -> tuple[ExplainResult, CallRecord]: ...
    async def classify_news(self, req: NewsRequest) -> tuple[NewsResult, CallRecord]: ...


class NullProvider:
    """AI_MODE=OFF: never called by the pipeline; any call fails loudly."""

    name, model = "null", ""

    async def review_trade(self, req: VetoRequest) -> tuple[VetoResult, CallRecord]:
        raise ProviderError("AI is off", CallRecord("VETO", self.name, "", req.model_dump()))

    async def rephrase(self, req: ExplainRequest) -> tuple[ExplainResult, CallRecord]:
        raise ProviderError("AI is off", CallRecord("EXPLAIN", self.name, "", req.model_dump()))

    async def classify_news(self, req: NewsRequest) -> tuple[NewsResult, CallRecord]:
        raise ProviderError("AI is off",
                            CallRecord("NEWS_SENTIMENT", self.name, "", req.model_dump()))


class _NotImplementedProvider:
    name, model = "stub", ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(f"{type(self).__name__} is a V2 stub")


class OpenAIProvider(_NotImplementedProvider):
    pass


class LocalModelProvider(_NotImplementedProvider):
    pass


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, *, timeout_seconds: float = 10,
                 client: httpx.AsyncClient | None = None, url: str = ANTHROPIC_URL) -> None:
        if not model:
            raise ValueError("ai.model is required when AI_MODE is not OFF")
        self._key = api_key
        self.model = model
        self.timeout = timeout_seconds
        self.url = url
        self._client = client or httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, purpose: str, system: str, user: str, schema: type[BaseModel],
                    max_tokens: int = 600) -> tuple[Any, CallRecord]:
        body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        rec = CallRecord(purpose, self.name, self.model, {"system": system, "user": user,
                                                          "max_tokens": max_tokens})
        started = time.monotonic()
        try:
            resp = await asyncio.wait_for(self._client.post(
                self.url, json=body, timeout=self.timeout,
                headers={"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION,
                         "content-type": "application/json"}), self.timeout + 2)
        except (httpx.HTTPError, TimeoutError) as exc:
            rec.latency_ms = int((time.monotonic() - started) * 1000)
            rec.error = f"{type(exc).__name__}: {exc}"[:500]
            raise ProviderError(rec.error, rec) from exc
        rec.latency_ms = int((time.monotonic() - started) * 1000)
        rec.response_raw = resp.text[:20000]
        if resp.status_code != 200:
            rec.error = f"HTTP {resp.status_code}"
            raise ProviderError(rec.error, rec)
        try:
            data = resp.json()
            rec.stop_reason = data.get("stop_reason")
            usage = data.get("usage") or {}
            rec.input_tokens, rec.output_tokens = usage.get("input_tokens"), usage.get(
                "output_tokens")
            if rec.stop_reason in ("refusal", "max_tokens"):
                raise ValueError(f"stop_reason {rec.stop_reason}")
            text = "".join(b.get("text", "") for b in data.get("content") or []
                           if b.get("type") == "text")
            parsed = schema.model_validate(_extract_json(text))
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            rec.error = f"{type(exc).__name__}: {exc}"[:500]
            raise ProviderError(rec.error, rec) from exc
        rec.parsed, rec.valid = parsed.model_dump(), True
        return parsed, rec

    async def review_trade(self, req: VetoRequest) -> tuple[VetoResult, CallRecord]:
        user = ("Proposed trade (structured data):\n"
                + json.dumps(req.model_dump(exclude={"news"}), sort_keys=True)
                + "\n<news>\n" + json.dumps([n.model_dump() for n in req.news]) + "\n</news>\n"
                + "Return only JSON: {\"verdict\": \"APPROVE\"|\"REJECT\", \"reasons\": [..], "
                  "\"contradictory_signals\": [..], \"unusual_conditions\": [..]}")
        result: VetoResult
        result, rec = await self._call("VETO", _prompt("veto_v1.md"), user, VetoResult)
        return result, rec

    async def rephrase(self, req: ExplainRequest) -> tuple[ExplainResult, CallRecord]:
        user = ("Template explanation:\n" + req.template + "\nFacts:\n"
                + json.dumps(req.facts, sort_keys=True) + "\nReturn only JSON: {\"text\": \"...\"}")
        result: ExplainResult
        result, rec = await self._call("EXPLAIN", _prompt("explain_v1.md"), user, ExplainResult)
        return result, rec

    async def classify_news(self, req: NewsRequest) -> tuple[NewsResult, CallRecord]:
        user = ("<news>\n" + json.dumps(req.model_dump()) + "\n</news>\nReturn only JSON: "
                "{\"sentiment\": \"POSITIVE\"|\"NEGATIVE\"|\"NEUTRAL\"|\"UNKNOWN\"}")
        result: NewsResult
        result, rec = await self._call("NEWS_SENTIMENT", _prompt("news_v1.md"), user,
                                       NewsResult, max_tokens=50)
        return result, rec
