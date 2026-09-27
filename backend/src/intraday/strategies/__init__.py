"""Trading strategies A/B/C (docs/design/09 §4)."""

from intraday.strategies.base import Strategy
from intraday.strategies.momentum_breakout import MomentumBreakout
from intraday.strategies.pullback import Pullback
from intraday.strategies.vwap_reclaim import VwapReclaim

ALL_STRATEGIES: tuple[Strategy, ...] = (MomentumBreakout(), VwapReclaim(), Pullback())
BY_NAME: dict[str, Strategy] = {s.name: s for s in ALL_STRATEGIES}

__all__ = ["ALL_STRATEGIES", "BY_NAME", "Strategy"]
