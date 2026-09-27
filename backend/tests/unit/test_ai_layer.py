"""Phase 9: provider (recorded-style fixtures via respx), number guard, news tags, minimisation."""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from intraday.ai import guard
from intraday.ai.news import blocked_tags, event_tags, news_factor
from intraday.ai.providers import (
    ANTHROPIC_URL,
    AnthropicProvider,
    ExplainRequest,
    LocalModelProvider,
    OpenAIProvider,
    ProviderError,
    VetoRequest,
)
from intraday.ai.service import veto_request


def veto_req() -> VetoRequest:
    return VetoRequest(symbol="AAPL", strategy="vwap_reclaim", signals=["reclaim"],
                       features={"rvol": 2.3}, factor_scores={"momentum": 0.7}, score=0.8,
                       stop_distance_pct=1.0, target_distance_pct=2.0, risk_reward=2.0,
                       regime={"label": "BULLISH"})


def reply(text: str, stop: str = "end_turn") -> httpx.Response:
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}],
                                     "stop_reason": stop,
                                     "usage": {"input_tokens": 120, "output_tokens": 30}})


@respx.mock
async def test_veto_approve_and_request_shape() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=reply(
        '```json\n{"verdict": "APPROVE", "reasons": ["clean setup"]}\n```'))
    p = AnthropicProvider("sk-test", "claude-sonnet-5")
    result, rec = await p.review_trade(veto_req())
    assert result.verdict == "APPROVE" and rec.valid and rec.input_tokens == 120
    req = route.calls[0].request
    assert req.headers["x-api-key"] == "sk-test" and req.headers["anthropic-version"]
    body = json.loads(req.content)
    assert body["model"] == "claude-sonnet-5" and "untrusted" in body["system"]


@pytest.mark.parametrize("response", [
    reply("not json at all"),
    reply('{"verdict": "MAYBE"}'),
    reply("", stop="refusal"),
    reply('{"verdict": "APPROVE"}', stop="max_tokens"),
    httpx.Response(500, text="overloaded"),
    httpx.Response(429, text="rate limited"),
])
@respx.mock
async def test_veto_failures_never_approve(response: httpx.Response) -> None:
    respx.post(ANTHROPIC_URL).mock(return_value=response)
    with pytest.raises(ProviderError) as exc:
        await AnthropicProvider("k", "m").review_trade(veto_req())
    assert exc.value.record.valid is False and exc.value.record.error


@respx.mock
async def test_timeout_is_a_failure() -> None:
    respx.post(ANTHROPIC_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError, match="ReadTimeout"):
        await AnthropicProvider("k", "m").rephrase(ExplainRequest(template="t", facts={}))


def test_provider_needs_model_and_stubs_raise() -> None:
    with pytest.raises(ValueError):
        AnthropicProvider("k", "")
    for cls in (OpenAIProvider, LocalModelProvider):
        with pytest.raises(NotImplementedError):
            cls()


def test_veto_request_contains_no_account_data() -> None:
    proposal = SimpleNamespace(entry_ref_price=100.0, stop_loss=99.0, take_profit=102.0,
                               signals=["breakout"], risk_reward_ratio=2.0,
                               market_context={"label": "BULLISH"})
    rec = SimpleNamespace(symbol="AAPL", strategy="momentum_breakout", score=0.8,
                          factor_scores={"momentum": 0.9}, proposal=proposal,
                          features={"price": 100.0, "rvol": 2.0, "equity": 1e5,
                                    "buying_power": 9e4, "account_id": "PA123"})
    news = [SimpleNamespace(headline="AAPL beats", event_tags=["earnings"],
                            published_at=__import__("datetime").datetime(2026, 9, 21))]
    payload = json.dumps(veto_request(rec, news).model_dump())
    for forbidden in ("equity", "buying_power", "account", "cash", "qty", "pnl", "order",
                      "PA123", "90000"):
        assert forbidden not in payload.lower(), forbidden
    assert '"stop_distance_pct": 1.0' in payload and "AAPL beats" in payload


def test_number_guard() -> None:
    template = "AAPL proposed: relative volume = 2.3x, stop $198.12, confidence 81%"
    facts: dict[str, Any] = {"rvol": 2.3, "stop": 198.12, "confidence": 0.81,
                             "vwap_dist_pct": 0.21}
    assert guard.check("Volume is 2.3x normal; the stop sits at $198.12 (81% confidence), "
                       "price above VWAP.", template, facts) == []
    assert guard.check("Stop at $197.50.", template, facts)                 # altered number
    assert guard.check("Price is below VWAP.", template, facts)             # flipped direction
    assert guard.check("A 5% gain is likely.", template, facts)             # invented number


def test_event_tags_and_blocking() -> None:
    assert event_tags("Apple to acquire startup", "Q3 earnings beat") == [
        "earnings", "merger_acquisition"]
    assert event_tags("Shares rise on product launch") == []
    assert "fda" in event_tags("FDA approves drug")
    items = [SimpleNamespace(symbols=["AAA"], event_tags=["earnings", "analyst_rating"])]
    assert blocked_tags(items, "AAA", ["earnings", "fda"]) == ("earnings",)
    assert blocked_tags(items, "BBB", ["earnings"]) == ()
    assert news_factor([]) == 0.5 and news_factor(["UNKNOWN"]) == 0.5
    assert news_factor(["POSITIVE", "NEGATIVE"]) == 0.5 and news_factor(["POSITIVE"]) == 0.8
