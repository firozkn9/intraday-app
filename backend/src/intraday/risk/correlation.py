"""Correlation clusters (SPEC §35; docs/design/07 §5). Pure.

Pearson correlation of 5-minute log returns over the lookback. A symbol with
too little data is treated as correlated with everything in its sector
(conservative). The candidate's cluster = held symbols correlated with it at
or above the threshold.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

MIN_COVERAGE = 0.6


def log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0]


def pearson(a: Sequence[float], b: Sequence[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return cov / math.sqrt(va * vb)


@dataclass(frozen=True)
class CorrelationModel:
    pairs: Mapping[frozenset[str], float] = field(default_factory=dict)
    insufficient: frozenset[str] = frozenset()
    sectors: Mapping[str, str | None] = field(default_factory=dict)

    def correlated(self, a: str, b: str, threshold: float) -> bool:
        if a == b:
            return True
        if a in self.insufficient or b in self.insufficient:
            sa, sb = self.sectors.get(a), self.sectors.get(b)
            return sa is not None and sa == sb
        c = self.pairs.get(frozenset((a, b)))
        return c is not None and c >= threshold

    def cluster(self, candidate: str, held: Iterable[str], threshold: float) -> set[str]:
        return {h for h in held if self.correlated(candidate, h, threshold)}

    def cluster_exposure(self, candidate: str, exposures: Mapping[str, Decimal],
                         threshold: float) -> Decimal:
        members = self.cluster(candidate, exposures.keys(), threshold)
        return sum((exposures[s] for s in members), Decimal("0"))


def build(closes_by_symbol: Mapping[str, Sequence[float]], expected_points: int,
          sectors: Mapping[str, str | None], *,
          coverage: Mapping[str, int] | None = None) -> CorrelationModel:
    """``closes_by_symbol``: aligned 5-minute closes; ``expected_points`` = full coverage.

    ``coverage`` optionally gives the number of *real* (not forward-filled) points.
    """
    rets = {s: log_returns(c) for s, c in closes_by_symbol.items()}
    have = coverage or {s: len(c) for s, c in closes_by_symbol.items()}
    insufficient = frozenset(s for s in closes_by_symbol
                             if expected_points == 0
                             or have.get(s, 0) < MIN_COVERAGE * expected_points)
    pairs: dict[frozenset[str], float] = {}
    syms = sorted(rets)
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            c = pearson(rets[a], rets[b])
            if c is not None:
                pairs[frozenset((a, b))] = c
    return CorrelationModel(pairs, insufficient, dict(sectors))
