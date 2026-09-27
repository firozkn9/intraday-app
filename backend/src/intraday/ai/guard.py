"""Number guard for LLM-rephrased explanations (docs/design/09 §7.5). Pure.

Every number in the LLM text must appear in the template or the fact table
(after normalising $, %, x and thousands separators, compared at the
displayed precision), and direction words must agree with the sign of the
fact they describe. Otherwise the LLM text is discarded and the template is
used.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")
# direction word → (fact key, required sign)
DIRECTIONS = {
    r"above (the )?vwap": ("vwap_dist_pct", 1),
    r"below (the )?vwap": ("vwap_dist_pct", -1),
    r"gapp?ed up|gap up": ("gap_pct", 1),
    r"gapp?ed down|gap down": ("gap_pct", -1),
}


def _norm(tok: str) -> str | None:
    t = tok.replace("$", "").replace(",", "").replace("%", "").lstrip("+")
    try:
        return f"{float(t):.4f}".rstrip("0").rstrip(".")
    except ValueError:
        return None


def _variants(value: float) -> set[str]:
    out = set()
    for digits in range(0, 5):
        out.add(_norm(f"{value:.{digits}f}") or "")
        out.add(_norm(f"{value * 100:.{digits}f}") or "")    # fractions shown as percent
    out.add(_norm(str(abs(value))) or "")
    return out


def allowed_numbers(template: str, facts: Mapping[str, float | str | None]) -> set[str]:
    allowed = {n for n in (_norm(m) for m in NUMBER.findall(template)) if n}
    for v in facts.values():
        if isinstance(v, (int, float)):
            allowed |= _variants(float(v))
            allowed |= _variants(abs(float(v)))
        elif isinstance(v, str):
            allowed |= {n for n in (_norm(m) for m in NUMBER.findall(v)) if n}
    return allowed


def check(text: str, template: str, facts: Mapping[str, float | str | None]) -> list[str]:
    """Problems with the LLM text; empty means it may be shown."""
    problems = []
    allowed = allowed_numbers(template, facts)
    for tok in NUMBER.findall(text):
        n = _norm(tok)
        if n is not None and n not in allowed and n.lstrip("-") not in allowed:
            problems.append(f"number {tok} not in the facts")
    lower = text.lower()
    for pattern, (key, sign) in DIRECTIONS.items():
        if re.search(pattern, lower):
            v = facts.get(key)
            if isinstance(v, (int, float)) and v * sign < 0:
                problems.append(f"'{pattern}' contradicts {key}={v}")
    return problems
