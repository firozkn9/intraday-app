"""Runtime settings: validation, bounds, hard limits, and change policy (docs/design/06)."""

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from intraday.core.runtime_config import (
    FIELD_SPECS,
    POLICIES,
    ConfigError,
    TradingConfig,
    diff_configs,
    hard_limit_violations,
    is_loosening,
    load_defaults,
    locked_violations,
    parse_config,
    validate_full,
)

DEFAULTS_PATH = Path(__file__).resolve().parents[3] / "config" / "defaults.yaml"
EQUITY = Decimal("100000")


def base() -> dict[str, Any]:
    return TradingConfig().to_json()


def with_change(key: str, value: Any) -> dict[str, Any]:
    data = base()
    node = data
    *path, leaf = key.split(".")
    for part in path:
        node = node[part]
    node[leaf] = value
    return data


def error_keys(data: dict[str, Any]) -> list[str]:
    with pytest.raises(ConfigError) as exc:
        validate_full(data, EQUITY)
    return [e.key for e in exc.value.errors]


# ------------------------------------------------------------------ defaults

def test_defaults_file_matches_model_defaults() -> None:
    assert load_defaults(DEFAULTS_PATH) == TradingConfig()


def test_claude_md_defaults() -> None:
    r = TradingConfig().risk
    assert TradingConfig().universe.min_stock_price == Decimal("10")
    assert r.paper_initial_balance == Decimal("100000")
    assert r.max_capital_per_trade == Decimal("10000")
    assert r.max_total_exposure == Decimal("50000")
    assert r.max_open_positions == 5
    assert r.max_daily_loss == Decimal("500")
    assert r.max_loss_per_trade == Decimal("100")
    assert TradingConfig().data.bar_interval == "1Min"
    assert TradingConfig().ai.mode == "OFF"
    assert r.allow_short_selling is False and r.allow_fractional_shares is False


def test_spec_section_4_values() -> None:
    c = TradingConfig()
    assert c.risk.max_daily_trades == 20
    assert c.risk.max_trades_per_symbol_per_day == 3
    assert c.exits.default_stop_loss_percent == 1.0
    assert c.exits.default_take_profit_percent == 2.0
    assert c.scoring.min_trade_score == 0.70
    assert (c.hours.trading_start, c.hours.no_new_trades_after, c.hours.force_close,
            c.hours.trading_end) == (time(9, 35), time(15, 30), time(15, 55), time(16, 0))


def test_defaults_pass_hard_limits() -> None:
    assert hard_limit_violations(TradingConfig(), EQUITY) == []


def test_json_round_trip() -> None:
    assert parse_config(TradingConfig().to_json()) == TradingConfig()


def test_yaml_round_trip() -> None:
    text = yaml.safe_dump(TradingConfig().to_json(), sort_keys=False)
    assert parse_config(yaml.safe_load(text)) == TradingConfig()


# ------------------------------------------------------------------ field bounds

@pytest.mark.parametrize(("key", "value"), [
    ("risk.max_open_positions", 21),
    ("risk.max_open_positions", 0),
    ("exits.default_stop_loss_percent", 0.1),
    ("exits.default_stop_loss_percent", 5.1),
    ("universe.min_stock_price", "4.99"),
    ("risk.max_daily_trades", 101),
    ("scoring.min_trade_score", 1.5),
    ("risk.correlation_threshold", 0.99),
    ("hours.early_close_force_close_offset_min", 1),
])
def test_out_of_bounds_rejected(key: str, value: Any) -> None:
    assert key in error_keys(with_change(key, value))


def test_unknown_keys_rejected() -> None:
    data = base()
    data["risk"]["max_leverage"] = 4
    assert "risk.max_leverage" in error_keys(data)


@pytest.mark.parametrize(("key", "value"), [
    ("risk.allow_short_selling", True),
    ("risk.allow_fractional_shares", True),
    ("data.bar_interval", "5Min"),
])
def test_locked_values_rejected(key: str, value: Any) -> None:
    assert key in error_keys(with_change(key, value))


def test_no_toggle_enables_etfs() -> None:
    """CLAUDE.md "Tradable assets": no settings toggle can enable non-common-stock assets."""
    assert not any("etf" in key.lower() or "asset" in key.lower() for key in FIELD_SPECS)


# ------------------------------------------------------------------ cross-field rules

def test_loss_per_trade_cannot_exceed_daily_loss() -> None:
    data = with_change("risk.max_loss_per_trade", "600")
    assert "risk" in error_keys(data)


def test_exposure_must_cover_one_trade() -> None:
    data = with_change("risk.max_total_exposure", "5000")
    assert "risk" in error_keys(data)


@pytest.mark.parametrize(("key", "value"), [
    ("hours.trading_start", "09:25"),
    ("hours.no_new_trades_after", "16:00"),
    ("hours.force_close", "15:59"),
    ("hours.force_close", "15:20"),
])
def test_hours_ordering(key: str, value: str) -> None:
    assert "hours" in error_keys(with_change(key, value))


def test_force_close_hard_limit_boundary() -> None:
    validate_full(with_change("hours.force_close", "15:58"), EQUITY)


def test_scoring_weights_must_sum_to_one() -> None:
    keys = error_keys(with_change("scoring.weight_news", 0.10))
    assert keys == ["scoring"]


def test_ai_model_required_when_enabled() -> None:
    assert "ai" in error_keys(with_change("ai.mode", "VETO"))
    data = with_change("ai.mode", "EXPLAIN")
    data["ai"]["model"] = "some-model-id"
    validate_full(data, EQUITY)


# ------------------------------------------------------------------ hard limits

@pytest.mark.parametrize(("key", "value"), [
    ("risk.max_loss_per_trade", "400"),     # > 1% of 20k
    ("risk.max_daily_loss", "700"),         # > 3% of 20k
    ("risk.max_capital_per_trade", "6000"),  # > 25% of 20k
])
def test_hard_limits_scale_with_equity(key: str, value: str) -> None:
    data = with_change(key, value)
    data["risk"]["max_total_exposure"] = "20000"
    data["risk"]["max_sector_exposure"] = "20000"
    data["risk"]["max_correlated_exposure"] = "20000"
    data["risk"]["max_capital_per_trade"] = data["risk"]["max_capital_per_trade"] \
        if key == "risk.max_capital_per_trade" else "5000"
    violations = hard_limit_violations(parse_config(data), Decimal("20000"))
    assert key in [v.key for v in violations]


def test_total_exposure_cannot_exceed_equity() -> None:
    violations = hard_limit_violations(TradingConfig(), Decimal("40000"))
    assert "risk.max_total_exposure" in [v.key for v in violations]


# ------------------------------------------------------------------ change policy

def test_every_field_has_a_valid_policy() -> None:
    assert len(FIELD_SPECS) > 90
    for spec in FIELD_SPECS.values():
        assert spec.policy in POLICIES, spec.key
        assert spec.description, spec.key


def test_every_paused_field_declares_a_loosening_direction() -> None:
    missing = [k for k, s in FIELD_SPECS.items() if s.policy == "PAUSED" and not s.loosen]
    assert missing == []


@pytest.mark.parametrize(("key", "old", "new", "loosening"), [
    ("risk.max_daily_loss", Decimal("500"), Decimal("600"), True),
    ("risk.max_daily_loss", Decimal("500"), Decimal("400"), False),
    ("scoring.min_trade_score", 0.70, 0.60, True),
    ("scoring.min_trade_score", 0.70, 0.80, False),
    ("risk.slippage_buffer_bps", 5, 2, True),
    ("hours.force_close", time(15, 55), time(15, 57), True),
    ("hours.force_close", time(15, 55), time(15, 50), False),
    ("regime.unknown_policy", "DISABLE", "REDUCE", True),
    ("regime.unknown_policy", "REDUCE", "DISABLE", False),
    ("news.block_event_tags", ["earnings", "fda"], ["earnings"], True),
    ("news.block_event_tags", ["earnings"], ["earnings", "fda"], False),
    ("exits.default_take_profit_percent", 2.0, 3.0, False),  # HOT, no direction
])
def test_is_loosening(key: str, old: Any, new: Any, loosening: bool) -> None:
    assert is_loosening(FIELD_SPECS[key], old, new) is loosening


def test_diff_and_requires_pause() -> None:
    old = TradingConfig()
    new = parse_config(with_change("risk.max_open_positions", 6))
    (change,) = diff_configs(old, new)
    assert (change.key, change.loosening, change.requires_pause) == \
        ("risk.max_open_positions", True, True)

    tighter = parse_config(with_change("risk.max_open_positions", 4))
    (change,) = diff_configs(old, tighter)
    assert change.loosening is False and change.requires_pause is False


def test_paused_any_always_requires_pause() -> None:
    data = with_change("ai.mode", "EXPLAIN")
    data["ai"]["model"] = "m"
    changes = {c.key: c for c in diff_configs(TradingConfig(), parse_config(data))}
    assert changes["ai.mode"].requires_pause is True
    assert changes["ai.model"].requires_pause is False


def test_restart_policy_flagged() -> None:
    (change,) = diff_configs(TradingConfig(), parse_config(with_change("data.feed", "sip")))
    assert change.policy == "RESTART" and change.requires_pause is False


def test_locked_violations_reported() -> None:
    from intraday.core.runtime_config import Change

    changes = [Change("data.bar_interval", "1Min", "5Min", "LOCKED", False)]
    assert [e.key for e in locked_violations(changes)] == ["data.bar_interval"]
