"""Paper guard (CLAUDE.md absolute rule; docs/design/08 §2)."""

import pytest

from intraday.core.safety import (
    PAPER_BASE_URL,
    PaperModeViolation,
    assert_paper_mode,
    normalize_base_url,
    paper_mode_problems,
)
from intraday.trading.broker.future_live import FutureLiveBroker


@pytest.mark.parametrize("url", [
    "https://paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets/",
    "  https://paper-api.alpaca.markets  ",
    "HTTPS://PAPER-API.ALPACA.MARKETS",
    "https://paper-api.alpaca.markets:443",
])
def test_paper_endpoint_variants_accepted(url: str) -> None:
    assert paper_mode_problems("PAPER", url) == []
    assert_paper_mode("PAPER", url)


@pytest.mark.parametrize("url", [
    "https://api.alpaca.markets",                       # live endpoint
    "https://api.alpaca.markets/",
    "http://paper-api.alpaca.markets",                  # not https
    "https://paper-api.alpaca.markets.evil.example",    # lookalike host
    "https://paper-api.alpaca.markets@api.alpaca.markets",
    "https://user:pw@paper-api.alpaca.markets",
    "https://paper-api.alpaca.markets:8443",
    "https://paper-api.alpaca.markets/v2",
    "https://paper-api.alpaca.markets/?x=1",
    "",
    "paper-api.alpaca.markets",
])
def test_non_paper_urls_refused(url: str) -> None:
    problems = paper_mode_problems("PAPER", url)
    assert problems, url
    with pytest.raises(PaperModeViolation):
        assert_paper_mode("PAPER", url)


@pytest.mark.parametrize("mode", ["LIVE", "paper", "PAPER ", "", "Paper"])
def test_trading_mode_must_be_exactly_paper(mode: str) -> None:
    problems = paper_mode_problems(mode, PAPER_BASE_URL)
    assert len(problems) == 1 and "TRADING_MODE" in problems[0]


def test_all_problems_reported_together() -> None:
    assert len(paper_mode_problems("LIVE", "https://api.alpaca.markets")) == 2


def test_normalize() -> None:
    assert normalize_base_url("https://Paper-API.alpaca.markets/") == PAPER_BASE_URL


def test_future_live_broker_cannot_be_constructed() -> None:
    with pytest.raises(NotImplementedError, match="paper trading only"):
        FutureLiveBroker()
