"""Absolute bounds no runtime setting can exceed (docs/design/06 §5).

Changing anything here is a code change that goes through review. The settings
UI and API validate against these; the risk engine (Phase 5) re-checks them.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import Final

HARD_MIN_STOCK_PRICE: Final = Decimal("5")
HARD_MAX_LOSS_PER_TRADE_PCT_OF_EQUITY: Final = Decimal("0.01")
HARD_MAX_DAILY_LOSS_PCT_OF_EQUITY: Final = Decimal("0.03")
HARD_MAX_TOTAL_EXPOSURE_MULTIPLE_OF_EQUITY: Final = Decimal("1.0")
HARD_MAX_CAPITAL_PER_TRADE_PCT_OF_EQUITY: Final = Decimal("0.25")
HARD_MAX_OPEN_POSITIONS: Final = 20
HARD_MIN_STOP_LOSS_PERCENT: Final = 0.2
HARD_MAX_STOP_LOSS_PERCENT: Final = 5.0
# Regular close is 16:00 ET; force-close must leave >= 2 minutes for retries.
HARD_LATEST_FORCE_CLOSE: Final = time(15, 58)
HARD_MIN_EARLY_CLOSE_FORCE_OFFSET_MIN: Final = 2
REGULAR_MARKET_CLOSE: Final = time(16, 0)
REGULAR_MARKET_OPEN: Final = time(9, 30)
