"""Deterministic news event tags and the news factor (docs/design/09 §9). Pure.

Tags come from keyword rules on headline + summary. Tags listed in
``news.block_event_tags`` block the symbol for the session (risk check R23).
Sentiment stays UNKNOWN unless the LLM classified it (VETO mode only); the
news factor is then 0.5 (neutral), so news alone can never create a trade.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

EVENT_RULES: dict[str, str] = {
    "earnings": r"\bearnings\b|\bquarterly results\b|\bq[1-4] (results|revenue|eps)\b|\beps\b",
    "guidance": r"\bguidance\b|\boutlook\b|\bforecast (raised|cut|lowered)\b",
    "fda": r"\bfda\b|\bclinical trial\b|\bphase (i{1,3}|[123])\b",
    "merger_acquisition": r"\bmerger\b|\bacquisition\b|\bto acquire\b|\bacquires\b|\bbuyout\b"
                          r"|\btakeover\b",
    "analyst_rating": r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|\bprice target\b",
    "lawsuit": r"\blawsuit\b|\bsued\b|\bclass action\b|\bsettlement\b",
    "regulatory": r"\bsec (probe|investigation|charges)\b|\bantitrust\b|\bregulator[sy]?\b",
    "offering": r"\bpublic offering\b|\bsecondary offering\b|\bshare sale\b|\bdilut",
    "trading_halt": r"\bhalt(ed)?\b|\bsuspend(ed)? trading\b",
}
_COMPILED = {tag: re.compile(rx, re.IGNORECASE) for tag, rx in EVENT_RULES.items()}
SENTIMENT_SCORE = {"POSITIVE": 0.8, "NEUTRAL": 0.5, "NEGATIVE": 0.2, "UNKNOWN": 0.5}


def event_tags(headline: str, summary: str | None = None) -> list[str]:
    text = f"{headline} {summary or ''}"
    return [tag for tag, rx in _COMPILED.items() if rx.search(text)]


def blocked_tags(items: Iterable[Any], symbol: str, block: Sequence[str]) -> tuple[str, ...]:
    tags = {t for n in items if symbol in n.symbols for t in n.event_tags}
    return tuple(sorted(t for t in tags if t in block))


def news_factor(sentiments: Sequence[str]) -> float:
    """Mean of known sentiments; 0.5 when there is no (classified) news."""
    known = [SENTIMENT_SCORE[s] for s in sentiments if s in SENTIMENT_SCORE and s != "UNKNOWN"]
    return round(sum(known) / len(known), 4) if known else 0.5
