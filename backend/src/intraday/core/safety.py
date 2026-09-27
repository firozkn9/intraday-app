"""Paper-trading guard (docs/design/08-trading-engine.md §2).

The engine calls ``assert_paper_mode`` at boot and the broker factory calls it
again before constructing a client. Neither depends on the UI.
"""

from __future__ import annotations

from urllib.parse import urlsplit

PAPER_MODE = "PAPER"
PAPER_BASE_URL = "https://paper-api.alpaca.markets"
_PAPER_HOST = "paper-api.alpaca.markets"


class PaperModeViolation(RuntimeError):
    """Raised when configuration would allow anything other than paper trading."""


def normalize_base_url(url: str) -> str:
    """Canonical form of an Alpaca base URL, or raise if it has anything unexpected."""
    raw = url.strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise PaperModeViolation(f"Alpaca base URL must use https, got {raw!r}")
    if parts.username or parts.password:
        raise PaperModeViolation("Alpaca base URL must not contain credentials")
    host = (parts.hostname or "").lower()
    if parts.port not in (None, 443):
        raise PaperModeViolation(f"Alpaca base URL must not set a custom port, got {raw!r}")
    if parts.path.rstrip("/") not in ("",):
        raise PaperModeViolation(f"Alpaca base URL must not contain a path, got {raw!r}")
    if parts.query or parts.fragment:
        raise PaperModeViolation(f"Alpaca base URL must not contain query/fragment, got {raw!r}")
    return f"https://{host}"


def paper_mode_problems(trading_mode: str, alpaca_base_url: str) -> list[str]:
    """Every reason the configuration is not safe paper trading (empty list = OK)."""
    problems: list[str] = []
    if trading_mode != PAPER_MODE:
        problems.append(f"TRADING_MODE must be exactly {PAPER_MODE!r}, got {trading_mode!r}")
    try:
        normalized = normalize_base_url(alpaca_base_url)
    except PaperModeViolation as exc:
        problems.append(str(exc))
    else:
        if normalized != PAPER_BASE_URL:
            problems.append(
                f"ALPACA_BASE_URL must be the paper endpoint {PAPER_BASE_URL}, "
                f"got {alpaca_base_url!r}"
            )
    return problems


def assert_paper_mode(trading_mode: str, alpaca_base_url: str) -> None:
    problems = paper_mode_problems(trading_mode, alpaca_base_url)
    if problems:
        raise PaperModeViolation("; ".join(problems))
