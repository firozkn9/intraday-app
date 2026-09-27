"""Runtime trading settings: model, validation, hard limits, and change policy.

See docs/design/06-configuration-model.md. Every leaf field carries metadata:

- ``policy``: when a change takes effect.
  HOT          next decision cycle
  PAUSED       *loosening* requires the engine to be paused (tightening is HOT)
  PAUSED_ANY   any change requires the engine to be paused
  RESTART      takes effect on next engine start / session open
  LOCKED       cannot change (fixed by CLAUDE.md / spec)
- ``loosen``: which direction relaxes risk: ``higher``, ``lower``, ``later``,
  ``order`` (enum, tight→loose in ``order``), ``subset`` (removing list items
  loosens), or absent (no risk direction).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import annotated_types
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.fields import FieldInfo

from intraday.core import hard_limits as hl

Policy = Literal["HOT", "PAUSED", "PAUSED_ANY", "RESTART", "LOCKED"]
POLICIES: tuple[Policy, ...] = ("HOT", "PAUSED", "PAUSED_ANY", "RESTART", "LOCKED")


def meta(
    policy: Policy,
    description: str,
    *,
    loosen: str | None = None,
    order: list[str] | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    extra: dict[str, Any] = {"policy": policy, "description": description}
    if loosen:
        extra["loosen"] = loosen
    if order:
        extra["order"] = order
    if unit:
        extra["unit"] = unit
    return extra


def F(default: Any, policy: Policy, description: str, **kw: Any) -> Any:
    """Field with change-policy metadata. ``kw`` may hold ge/le/gt/lt and meta args."""
    meta_kw = {k: kw.pop(k) for k in ("loosen", "order", "unit") if k in kw}
    return Field(default, json_schema_extra=meta(policy, description, **meta_kw), **kw)


Money = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskSettings(_Group):
    paper_initial_balance: Money = F(
        Decimal("100000"), "HOT", "Starting capital for backtests and replay. In paper "
        "trading the broker account is the source of truth.", ge=1000, le=10_000_000, unit="USD")
    max_capital_per_trade: Money = F(
        Decimal("10000"), "PAUSED", "Maximum position value for one trade.",
        ge=100, loosen="higher", unit="USD")
    max_total_exposure: Money = F(
        Decimal("50000"), "PAUSED", "Maximum combined value of open positions and pending "
        "entries.", ge=100, loosen="higher", unit="USD")
    max_open_positions: int = F(
        5, "PAUSED", "Maximum open positions (including pending entries).",
        ge=1, le=hl.HARD_MAX_OPEN_POSITIONS, loosen="higher")
    max_daily_loss: Money = F(
        Decimal("500"), "PAUSED", "Daily loss (realized + unrealized) that locks trading.",
        ge=10, loosen="higher", unit="USD")
    max_loss_per_trade: Money = F(
        Decimal("100"), "PAUSED", "Maximum loss if a trade's stop is hit (incl. slippage "
        "buffer).", ge=1, loosen="higher", unit="USD")
    max_daily_trades: int = F(20, "PAUSED", "Maximum entries per session.",
                              ge=1, le=100, loosen="higher")
    max_trades_per_symbol_per_day: int = F(3, "PAUSED", "Maximum entries per symbol per session.",
                                           ge=1, le=10, loosen="higher")
    max_sector_exposure: Money = F(Decimal("20000"), "PAUSED", "Maximum exposure to one sector.",
                                   ge=0, loosen="higher", unit="USD")
    max_correlated_exposure: Money = F(
        Decimal("20000"), "PAUSED", "Maximum exposure to one correlation cluster.",
        ge=0, loosen="higher", unit="USD")
    correlation_threshold: float = F(
        0.70, "PAUSED", "Correlation at or above which two symbols count as one cluster.",
        ge=0.3, le=0.95, loosen="higher")
    correlation_lookback_days: int = F(20, "HOT", "Lookback for correlation clusters.",
                                       ge=5, le=60, unit="days")
    min_risk_reward: float = F(1.5, "PAUSED", "Minimum reward/risk ratio.",
                               ge=1.0, le=5.0, loosen="lower")
    buying_power_utilization: float = F(0.90, "PAUSED", "Fraction of buying power usable.",
                                        ge=0.1, le=1.0, loosen="higher")
    slippage_buffer_bps: int = F(5, "PAUSED", "Added to risk per share when sizing.",
                                 ge=0, le=50, loosen="lower", unit="bps")
    min_position_value: Money = F(Decimal("500"), "HOT", "Smallest position worth opening.",
                                  ge=0, unit="USD")
    reentry_cooldown_minutes: int = F(15, "PAUSED", "Wait before re-entering a symbol after "
                                      "an exit.", ge=0, le=240, loosen="lower", unit="min")
    daily_loss_action: Literal["KEEP_STOPS", "FLATTEN"] = F(
        "KEEP_STOPS", "HOT", "On daily loss limit: keep protective stops, or close all.")
    emergency_stop_action: Literal["CLOSE_ALL", "KEEP_STOPS"] = F(
        "CLOSE_ALL", "HOT", "Default for EMERGENCY STOP: close all positions or keep stops.")
    daily_loss_basis: Literal["WORST_CASE_OPEN_RISK", "CURRENT_PNL"] = F(
        "WORST_CASE_OPEN_RISK", "PAUSED", "How the pre-trade daily loss check counts open risk.",
        loosen="order", order=["WORST_CASE_OPEN_RISK", "CURRENT_PNL"])
    allow_short_selling: Literal[False] = F(False, "LOCKED", "Long only (CLAUDE.md).")
    allow_fractional_shares: Literal[False] = F(
        False, "LOCKED", "Whole shares only: broker bracket orders don't support fractions.")

    @model_validator(mode="after")
    def _cross(self) -> RiskSettings:
        if self.max_total_exposure < self.max_capital_per_trade:
            raise ValueError("max_total_exposure must be >= max_capital_per_trade")
        if self.max_loss_per_trade > self.max_daily_loss:
            raise ValueError("max_loss_per_trade must be <= max_daily_loss")
        if self.max_sector_exposure > self.max_total_exposure:
            raise ValueError("max_sector_exposure must be <= max_total_exposure")
        if self.max_correlated_exposure > self.max_total_exposure:
            raise ValueError("max_correlated_exposure must be <= max_total_exposure")
        if self.min_position_value > self.max_capital_per_trade:
            raise ValueError("min_position_value must be <= max_capital_per_trade")
        return self


class ExitSettings(_Group):
    default_stop_loss_percent: float = F(
        1.0, "PAUSED", "Default stop distance from entry.",
        ge=hl.HARD_MIN_STOP_LOSS_PERCENT, le=hl.HARD_MAX_STOP_LOSS_PERCENT,
        loosen="higher", unit="%")
    default_take_profit_percent: float = F(2.0, "HOT", "Default target distance from entry.",
                                           ge=0.2, le=20, unit="%")
    trailing_enabled: bool = F(True, "HOT", "Trail the stop after the position gains.")
    trailing_activation_r: float = F(1.0, "HOT", "Gain (in R) that activates trailing.",
                                     ge=0.25, le=5)
    trailing_distance_percent: float = F(0.75, "HOT", "Trailing distance below the high.",
                                         ge=0.2, le=5, unit="%")
    max_holding_minutes: int = F(90, "HOT", "Time-based exit.", ge=5, le=390, unit="min")
    momentum_reversal_enabled: bool = F(True, "HOT", "Exit on EMA9/EMA20 + MACD reversal.")
    vwap_loss_enabled: bool = F(True, "HOT", "Exit after consecutive closes below VWAP.")
    vwap_loss_bars: int = F(2, "HOT", "Closes below VWAP that trigger the exit.", ge=1, le=10)
    regime_reversal_enabled: bool = F(True, "HOT", "Exit when the market regime turns hostile.")


class HoursSettings(_Group):
    trading_start: time = F(time(9, 35), "HOT", "Entry window opens (ET).")
    no_new_trades_after: time = F(time(15, 30), "HOT", "Entry window closes (ET).")
    force_close: time = F(time(15, 55), "PAUSED", "Liquidate all positions (ET).",
                          loosen="later")
    trading_end: time = F(time(16, 0), "HOT", "Monitoring ends (ET).")
    early_close_no_new_trades_offset_min: int = F(
        30, "HOT", "On early-close days: stop entries this long before the close.",
        ge=5, le=120, unit="min")
    early_close_force_close_offset_min: int = F(
        5, "PAUSED", "On early-close days: force-close this long before the close.",
        ge=hl.HARD_MIN_EARLY_CLOSE_FORCE_OFFSET_MIN, le=60, loosen="lower", unit="min")

    @model_validator(mode="after")
    def _order(self) -> HoursSettings:
        if not (hl.REGULAR_MARKET_OPEN <= self.trading_start < self.no_new_trades_after
                < self.force_close < self.trading_end <= hl.REGULAR_MARKET_CLOSE):
            raise ValueError(
                "hours must satisfy 09:30 <= trading_start < no_new_trades_after < "
                "force_close < trading_end <= 16:00")
        if self.force_close > hl.HARD_LATEST_FORCE_CLOSE:
            raise ValueError("force_close must be at or before 15:58 ET (hard limit)")
        if self.early_close_force_close_offset_min >= self.early_close_no_new_trades_offset_min:
            raise ValueError("early-close force-close offset must be smaller than the "
                             "no-new-trades offset")
        return self


class UniverseSettings(_Group):
    min_stock_price: Money = F(Decimal("10"), "PAUSED", "Minimum price to trade.",
                               ge=hl.HARD_MIN_STOCK_PRICE, loosen="lower", unit="USD")
    min_average_daily_volume: int = F(
        20000, "PAUSED", "Minimum average daily volume, in units of the configured feed "
        "(IEX sees ~2-3% of consolidated volume).", ge=0, loosen="lower", unit="shares")
    adv_lookback_days: int = F(20, "HOT", "Lookback for average daily volume.", ge=5, le=60,
                               unit="days")
    min_market_cap: Money = F(Decimal("2000000000"), "PAUSED", "Applied when the watchlist "
                              "entry has a market cap.", ge=0, loosen="lower", unit="USD")
    max_spread_percent_iex: float = F(0.30, "PAUSED", "Maximum bid/ask spread on IEX quotes.",
                                      gt=0, le=2, loosen="higher", unit="%")
    max_spread_percent_sip: float = F(0.10, "PAUSED", "Maximum bid/ask spread on SIP quotes.",
                                      gt=0, le=2, loosen="higher", unit="%")
    min_bar_coverage: float = F(0.80, "PAUSED", "Fraction of the last 30 minutes with a bar.",
                                ge=0.3, le=1.0, loosen="lower")
    max_watchlist_size: int = F(20, "HOT", "Tradable symbols (stream budget).", ge=1, le=28)
    scan_interval_minutes: int = F(5, "HOT", "How often the universe scan runs during the "
                                   "session (also at 09:25 ET and on demand).",
                                   ge=1, le=60, unit="min")


StopMethod = Literal["PERCENT", "ATR", "STRUCTURE"]


class _StrategyBase(_Group):
    enabled: bool = F(True, "HOT", "Strategy may propose trades.")
    stop_method: StopMethod = F("PERCENT", "HOT", "How the stop is placed.")
    atr_multiple: float = F(1.5, "HOT", "ATR multiple for ATR stops.", ge=0.5, le=5)
    max_stop_percent: float = F(2.0, "PAUSED", "Cap on stop distance for ATR/STRUCTURE.",
                                ge=hl.HARD_MIN_STOP_LOSS_PERCENT,
                                le=hl.HARD_MAX_STOP_LOSS_PERCENT, loosen="higher", unit="%")


class MomentumBreakoutSettings(_StrategyBase):
    breakout_lookback_bars: int = F(15, "HOT", "Bars defining the recent high.", ge=3, le=120)
    min_rvol: float = F(1.5, "HOT", "Minimum relative volume.", ge=0.5, le=10)
    min_volume_increase_bars: int = F(3, "HOT", "Consecutive rising-volume bars.", ge=1, le=10)


class VwapReclaimSettings(_StrategyBase):
    min_bars_below_vwap: int = F(5, "HOT", "Prior closes below VWAP.", ge=1, le=60)
    reclaim_confirm_bars: int = F(1, "HOT", "Closes above VWAP to confirm.", ge=1, le=5)
    min_rvol: float = F(1.2, "HOT", "Minimum relative volume.", ge=0.5, le=10)
    require_rsi_above: float = F(50, "HOT", "RSI confirmation level.", ge=0, le=100)


class PullbackSettings(_StrategyBase):
    trend_min_bars: int = F(20, "HOT", "Bars of EMA alignment defining the trend.", ge=5, le=200)
    max_pullback_percent: float = F(1.5, "HOT", "Maximum pullback from the recent high.",
                                    ge=0.1, le=10, unit="%")
    support: Literal["EMA20", "VWAP"] = F("EMA20", "HOT", "Support level for the pullback.")
    support_tolerance_percent: float = F(0.2, "HOT", "How close the low must get to support.",
                                         ge=0, le=2, unit="%")
    min_rvol: float = F(1.0, "HOT", "Minimum relative volume.", ge=0.3, le=10)


class StrategySettings(_Group):
    momentum_breakout: MomentumBreakoutSettings = MomentumBreakoutSettings()
    vwap_reclaim: VwapReclaimSettings = VwapReclaimSettings()
    pullback: PullbackSettings = PullbackSettings()


class ScoringSettings(_Group):
    weight_momentum: float = F(0.20, "HOT", "Weight of momentum factor.", ge=0, le=1)
    weight_volume: float = F(0.20, "HOT", "Weight of volume factor.", ge=0, le=1)
    weight_price_action: float = F(0.20, "HOT", "Weight of price-action factor.", ge=0, le=1)
    weight_market_regime: float = F(0.15, "HOT", "Weight of market-regime factor.", ge=0, le=1)
    weight_volatility: float = F(0.10, "HOT", "Weight of volatility factor.", ge=0, le=1)
    weight_technical: float = F(0.10, "HOT", "Weight of technical factor.", ge=0, le=1)
    weight_news: float = F(0.05, "HOT", "Weight of news factor.", ge=0, le=1)
    min_trade_score: float = F(0.70, "PAUSED", "Minimum score (model confidence) to propose "
                               "a trade. A score, not a probability of profit.",
                               ge=0.3, le=1.0, loosen="lower")

    @property
    def weights(self) -> dict[str, float]:
        return {k.removeprefix("weight_"): v for k, v in self.model_dump().items()
                if k.startswith("weight_")}

    @model_validator(mode="after")
    def _sum(self) -> ScoringSettings:
        total = sum(self.weights.values())
        if not math.isclose(total, 1.0, abs_tol=0.001):
            raise ValueError(f"scoring weights must sum to 1.00 (currently {total:.3f})")
        return self


class RegimeSettings(_Group):
    unknown_policy: Literal["DISABLE", "REDUCE"] = F(
        "DISABLE", "PAUSED", "Entries when the regime is UNKNOWN.",
        loosen="order", order=["DISABLE", "REDUCE"])
    bearish_policy: Literal["DISABLE", "REDUCE"] = F(
        "DISABLE", "PAUSED", "Entries when the regime is BEARISH (long only).",
        loosen="order", order=["DISABLE", "REDUCE"])
    high_volatility_size_multiplier: float = F(0.5, "PAUSED", "Size multiplier in HIGH_VOLATILITY.",
                                               ge=0.1, le=1.0, loosen="higher")
    reduce_size_multiplier: float = F(0.5, "PAUSED", "Size multiplier when a policy is REDUCE.",
                                      ge=0.1, le=1.0, loosen="higher")


class AISettings(_Group):
    mode: Literal["OFF", "EXPLAIN", "VETO"] = F(
        "OFF", "PAUSED_ANY", "OFF: no LLM calls. EXPLAIN: rephrases explanations only. "
        "VETO: may reject proposed trades, never approve or originate them.")
    provider: Literal["anthropic"] = F("anthropic", "RESTART", "LLM provider.")
    model: str = F("", "HOT", "Model ID. Required when mode is not OFF.", max_length=100)
    effort: Literal["low", "medium", "high"] = F("low", "HOT", "LLM effort level.")
    timeout_seconds: int = F(10, "HOT", "LLM call timeout.", ge=2, le=60, unit="s")
    max_consecutive_failures: int = F(3, "HOT", "Failures before AI_UNAVAILABLE (VETO only).",
                                      ge=1, le=20)
    news_sentiment_enabled: bool = F(True, "HOT", "LLM news sentiment (VETO mode only).")

    @model_validator(mode="after")
    def _model_required(self) -> AISettings:
        if self.mode != "OFF" and not self.model.strip():
            raise ValueError("ai.model is required when ai.mode is not OFF")
        return self


class DataSettings(_Group):
    feed: Literal["iex", "sip"] = F("iex", "RESTART", "Alpaca market-data feed.")
    stale_seconds: int = F(120, "PAUSED", "Bar age after which data counts as stale.",
                           ge=30, le=600, loosen="higher", unit="s")
    bar_interval: Literal["1Min"] = F("1Min", "LOCKED", "Bar interval (CLAUDE.md).")


class NewsSettings(_Group):
    enabled: bool = F(True, "HOT", "Fetch and tag news.")
    lookback_hours: int = F(24, "HOT", "News lookback.", ge=1, le=168, unit="h")
    block_event_tags: list[str] = F(
        ["earnings", "fda", "merger_acquisition", "trading_halt"], "PAUSED",
        "Event tags that block a symbol for the session.", loosen="subset")


class ExecutionSettings(_Group):
    entry_limit_offset_bps: int = F(5, "HOT", "Marketable-limit offset above the ask.",
                                    ge=0, le=50, unit="bps")
    entry_timeout_seconds: int = F(30, "HOT", "Cancel unfilled entry remainder after.",
                                   ge=5, le=300, unit="s")
    exit_fill_timeout_seconds: int = F(20, "HOT", "Escalate unfilled exits after.",
                                       ge=5, le=120, unit="s")
    reconcile_interval_seconds: int = F(30, "HOT", "Broker reconciliation interval.",
                                        ge=10, le=300, unit="s")


class AlertSettings(_Group):
    email_enabled: bool = F(True, "HOT", "Send alerts by email.")
    email_min_severity: Literal["INFO", "WARN", "HIGH", "CRITICAL"] = F(
        "HIGH", "HOT", "Lowest severity that is emailed.")


class TradingConfig(_Group):
    risk: RiskSettings = RiskSettings()
    exits: ExitSettings = ExitSettings()
    hours: HoursSettings = HoursSettings()
    universe: UniverseSettings = UniverseSettings()
    strategies: StrategySettings = StrategySettings()
    scoring: ScoringSettings = ScoringSettings()
    regime: RegimeSettings = RegimeSettings()
    ai: AISettings = AISettings()
    data: DataSettings = DataSettings()
    news: NewsSettings = NewsSettings()
    execution: ExecutionSettings = ExecutionSettings()
    alerts: AlertSettings = AlertSettings()

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------- loading

class ConfigError(ValueError):
    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e.key}: {e.message}" for e in errors))


@dataclass(frozen=True)
class FieldError:
    key: str
    message: str


def _pydantic_errors(exc: ValidationError) -> list[FieldError]:
    out = []
    for err in exc.errors():
        key = ".".join(str(p) for p in err["loc"]) or "(root)"
        msg = str(err["msg"]).removeprefix("Value error, ")
        out.append(FieldError(key, msg))
    return out


def parse_config(data: dict[str, Any]) -> TradingConfig:
    try:
        return TradingConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_pydantic_errors(exc)) from exc


def load_defaults(path: Path) -> TradingConfig:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_config(data)


def validate_full(data: dict[str, Any], equity: Decimal) -> TradingConfig:
    """Model validation + hard limits relative to account equity."""
    cfg = parse_config(data)
    errors = hard_limit_violations(cfg, equity)
    if errors:
        raise ConfigError(errors)
    return cfg


def hard_limit_violations(cfg: TradingConfig, equity: Decimal) -> list[FieldError]:
    r = cfg.risk
    errs: list[FieldError] = []

    def cap(key: str, value: Decimal, limit: Decimal, label: str) -> None:
        if value > limit:
            errs.append(FieldError(key, f"exceeds hard limit {label} (${limit:,.2f} at equity "
                                        f"${equity:,.2f})"))

    cap("risk.max_loss_per_trade", r.max_loss_per_trade,
        equity * hl.HARD_MAX_LOSS_PER_TRADE_PCT_OF_EQUITY, "1% of equity")
    cap("risk.max_daily_loss", r.max_daily_loss,
        equity * hl.HARD_MAX_DAILY_LOSS_PCT_OF_EQUITY, "3% of equity")
    cap("risk.max_total_exposure", r.max_total_exposure,
        equity * hl.HARD_MAX_TOTAL_EXPOSURE_MULTIPLE_OF_EQUITY, "1.0x equity")
    cap("risk.max_capital_per_trade", r.max_capital_per_trade,
        equity * hl.HARD_MAX_CAPITAL_PER_TRADE_PCT_OF_EQUITY, "25% of equity")
    return errs


# --------------------------------------------------------------------------- field metadata

@dataclass(frozen=True)
class FieldSpec:
    key: str
    group: str
    type: str
    default: Any
    policy: Policy
    description: str
    loosen: str | None = None
    order: tuple[str, ...] | None = None
    options: tuple[Any, ...] | None = None
    ge: float | None = None
    le: float | None = None
    gt: float | None = None
    lt: float | None = None
    unit: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if v is not None}
        d["default"] = _jsonable(self.default)
        if self.order:
            d["order"] = list(self.order)
        if self.options:
            d["options"] = list(self.options)
        return d


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, list):
        return list(v)
    return v


def _type_name(annotation: Any) -> tuple[str, tuple[Any, ...] | None]:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _type_name(get_args(annotation)[0])
    if origin is Literal:
        opts = get_args(annotation)
        if all(isinstance(o, bool) for o in opts):
            return "bool", opts
        return "enum", opts
    if origin in (list,):
        return "list[str]", None
    if origin in (Union, UnionType):
        raise TypeError(f"optional settings are not supported: {annotation}")
    mapping = {bool: "bool", int: "int", float: "float", Decimal: "decimal", time: "time",
               str: "str"}
    return mapping[annotation], None


def _bounds(info: FieldInfo) -> dict[str, float]:
    out: dict[str, float] = {}
    for m in info.metadata:
        for attr, cls in (("ge", annotated_types.Ge), ("le", annotated_types.Le),
                          ("gt", annotated_types.Gt), ("lt", annotated_types.Lt)):
            if isinstance(m, cls):
                out[attr] = float(getattr(m, attr))
    return out


def iter_field_specs(model: type[BaseModel] = TradingConfig, prefix: str = "",
                     group: str = "") -> Iterator[FieldSpec]:
    for name, info in model.model_fields.items():
        ann = info.annotation
        key = f"{prefix}{name}"
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            yield from iter_field_specs(ann, f"{key}.", group or name)
            continue
        extra = info.json_schema_extra
        if not isinstance(extra, dict):
            raise TypeError(f"setting {key} is missing policy metadata")
        type_name, options = _type_name(ann)
        order = extra.get("order")
        yield FieldSpec(
            key=key,
            group=group,
            type=type_name,
            default=info.default,
            policy=extra["policy"],  # type: ignore[arg-type]
            description=str(extra["description"]),
            loosen=extra.get("loosen"),  # type: ignore[arg-type]
            order=tuple(str(o) for o in order) if isinstance(order, list) else None,
            options=options,
            unit=extra.get("unit"),  # type: ignore[arg-type]
            **_bounds(info),
        )


FIELD_SPECS: dict[str, FieldSpec] = {s.key: s for s in iter_field_specs()}


# --------------------------------------------------------------------------- change policy

@dataclass(frozen=True)
class Change:
    key: str
    old: Any
    new: Any
    policy: Policy
    loosening: bool

    @property
    def requires_pause(self) -> bool:
        return self.policy == "PAUSED_ANY" or (self.policy == "PAUSED" and self.loosening)

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "old": _jsonable(self.old), "new": _jsonable(self.new),
                "policy": self.policy, "loosening": self.loosening,
                "requires_pause": self.requires_pause}


def _get(cfg: TradingConfig, key: str) -> Any:
    obj: Any = cfg
    for part in key.split("."):
        obj = getattr(obj, part)
    return obj


def is_loosening(spec: FieldSpec, old: Any, new: Any) -> bool:
    match spec.loosen:
        case "higher":
            return bool(new > old)
        case "lower":
            return bool(new < old)
        case "later":
            return bool(new > old)
        case "order":
            if spec.order is None:
                raise ValueError(f"{spec.key}: 'order' loosening needs an order list")
            return spec.order.index(new) > spec.order.index(old)
        case "subset":
            return bool(set(old) - set(new))
        case _:
            return False


def diff_configs(old: TradingConfig, new: TradingConfig) -> list[Change]:
    changes: list[Change] = []
    for key, spec in FIELD_SPECS.items():
        a, b = _get(old, key), _get(new, key)
        if a != b:
            changes.append(Change(key, a, b, spec.policy, is_loosening(spec, a, b)))
    return changes


def locked_violations(changes: list[Change]) -> list[FieldError]:
    return [FieldError(c.key, "this setting is locked and cannot change")
            for c in changes if c.policy == "LOCKED"]
