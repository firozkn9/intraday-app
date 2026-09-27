"""Indicators against hand-computed values, definitions, and properties."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from intraday.features import indicators as ind

prices = st.lists(st.floats(min_value=1, max_value=1000, allow_nan=False), min_size=40,
                  max_size=120)


def approx(a: float | None, b: float, tol: float = 1e-9) -> bool:
    return a is not None and abs(a - b) <= tol * max(1.0, abs(b))


def test_sma_and_ema_hand_values() -> None:
    v = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert ind.sma(v, 3) == [None, None, 2.0, 3.0, 4.0]
    e = ind.ema(v, 3)  # seed 2.0, alpha 0.5
    assert e[:2] == [None, None]
    assert e[2:] == [2.0, 3.0, 4.0]
    e2 = ind.ema([10.0, 10.0, 10.0, 16.0], 3)
    assert e2[3] == pytest.approx(13.0)


@given(prices)
def test_ema_matches_recursive_definition(values: list[float]) -> None:
    n = 9
    got = ind.ema(values, n)
    alpha = 2 / (n + 1)
    prev = sum(values[:n]) / n
    assert approx(got[n - 1], prev)
    for i in range(n, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        assert approx(got[i], prev, 1e-7)


def test_rsi_extremes_and_hand_example() -> None:
    up = [float(i) for i in range(1, 30)]
    assert ind.rsi(up, 14)[-1] == 100.0
    assert ind.rsi(list(reversed(up)), 14)[-1] == 0.0
    assert ind.rsi([5.0] * 30, 14)[-1] == 50.0
    # n=2: changes +1, -1 → avg gain 0.5, avg loss 0.5 → RSI 50; next +2:
    # gain (0.5*1+2)/2=1.25, loss (0.5*1+0)/2=0.25 → RS 5 → 83.333…
    r = ind.rsi([10.0, 11.0, 10.0, 12.0], 2)
    assert r[:2] == [None, None]
    assert r[2] == pytest.approx(50.0)
    assert r[3] == pytest.approx(100 - 100 / 6)


@given(prices)
def test_rsi_bounded(values: list[float]) -> None:
    assert all(0 <= x <= 100 for x in ind.rsi(values, 14) if x is not None)


def test_macd_constant_series_is_zero_and_alignment() -> None:
    line, sig, hist = ind.macd([50.0] * 60)
    assert line[24] is None and line[25] == pytest.approx(0.0)
    assert sig[32] is None and sig[33] == pytest.approx(0.0)  # 26 + 9 - 2
    assert hist[-1] == pytest.approx(0.0)


def test_macd_positive_in_uptrend() -> None:
    line, _, _ = ind.macd([100 + i * 0.5 for i in range(80)])
    assert line[-1] is not None and line[-1] > 0


def test_true_range_and_atr() -> None:
    h, lo, c = [10.0, 12.0, 11.0], [9.0, 10.0, 8.0], [9.5, 11.5, 9.0]
    assert ind.true_range(h, lo, c) == [1.0, 2.5, 3.5]  # gap-aware
    a = ind.atr(h, lo, c, 2)
    assert a == [None, 1.75, (1.75 * 1 + 3.5) / 2]


def test_roc_and_vwap() -> None:
    assert ind.roc([100.0, 105.0, 110.0], 2) == [None, None, pytest.approx(10.0)]
    assert ind.cumulative_vwap([10.0, 20.0], [1.0, 3.0]) == [10.0, 17.5]
    assert ind.cumulative_vwap([10.0], [0.0]) == [None]


def test_realized_vol_and_slope() -> None:
    assert ind.realized_vol([100.0, 100.0, 100.0]) == 0.0
    assert ind.realized_vol([100.0]) is None
    r = ind.realized_vol([100.0, 101.0, 100.0, 101.0])
    assert r == pytest.approx(math.log(1.01) * math.sqrt(4 / 3), rel=1e-6)
    assert ind.slope([1.0, 3.0, 5.0]) == pytest.approx(2.0)


@given(prices, st.integers(min_value=20, max_value=39))
def test_no_look_ahead(values: list[float], cut: int) -> None:
    """Changing the future never changes past indicator values."""
    future_changed = values[:cut] + [v * 1.5 for v in values[cut:]]
    for fn in (lambda x: ind.ema(x, 9), lambda x: ind.rsi(x, 14), lambda x: ind.macd(x)[2],
               lambda x: ind.roc(x, 10), lambda x: ind.sma(x, 5)):
        assert fn(values)[:cut] == fn(future_changed)[:cut]


def test_invalid_periods() -> None:
    with pytest.raises(ValueError):
        ind.ema([1.0], 0)
    with pytest.raises(ValueError):
        ind.macd([1.0] * 40, 26, 12)
