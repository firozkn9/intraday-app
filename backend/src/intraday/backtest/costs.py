"""Transaction costs and slippage assumptions (SPEC §24).

Defaults are assumptions to be reviewed, not quotes: commission-free equities,
U.S. SEC fee and FINRA TAF on sells only, and slippage in basis points on every
fill. Alpaca paper fills charge no fees; backtests use realistic non-zero costs
on purpose (docs/design/08 §10).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

CENT = Decimal("0.01")


class CostModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    commission_per_share: Decimal = Field(Decimal("0"), ge=0, le=Decimal("0.05"))
    sec_fee_per_dollar: Decimal = Field(Decimal("0.0000278"), ge=0, le=Decimal("0.001"),
                                        description="on sell notional")
    taf_per_share: Decimal = Field(Decimal("0.000166"), ge=0, le=Decimal("0.01"),
                                   description="on shares sold")
    taf_max_per_trade: Decimal = Field(Decimal("8.30"), ge=0)
    slippage_bps: int = Field(5, ge=0, le=100, description="applied against us on each fill")
    assumed_spread_pct: Decimal = Field(Decimal("0.05"), ge=0, le=Decimal("2"),
                                        description="used for the risk engine's spread check")

    def buy_fees(self, qty: int, price: Decimal) -> Decimal:
        return (self.commission_per_share * qty).quantize(CENT, rounding=ROUND_HALF_UP)

    def sell_fees(self, qty: int, price: Decimal) -> Decimal:
        sec = self.sec_fee_per_dollar * price * qty
        taf = min(self.taf_per_share * qty, self.taf_max_per_trade)
        return (self.commission_per_share * qty + sec + taf).quantize(CENT,
                                                                      rounding=ROUND_HALF_UP)
