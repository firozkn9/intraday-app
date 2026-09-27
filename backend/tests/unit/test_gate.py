"""Trading gate: priorities, latches, display status (docs/design/07 §6)."""

from datetime import UTC, datetime

import pytest

from intraday.core.gate import BLOCKS, Gate

T0 = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)


def test_empty_gate_is_running() -> None:
    g = Gate()
    assert g.entries_allowed and g.display_status() == "RUNNING"


def test_any_block_closes_the_gate() -> None:
    for spec in BLOCKS:
        g = Gate()
        g.set(spec.reason, T0)
        assert not g.entries_allowed, spec.reason


def test_highest_priority_block_wins() -> None:
    g = Gate()
    g.set("PAUSED_BY_USER", T0)
    g.set("DATA_STALE", T0)
    assert g.display_status() == "DATA ERROR"
    g.set("DAILY_LOSS_LIMIT", T0)
    assert g.display_status() == "DAILY LOSS LIMIT"
    g.set("EMERGENCY_STOP", T0)
    assert g.display_status() == "EMERGENCY STOP"


@pytest.mark.parametrize("reason", ["EMERGENCY_STOP", "DAILY_LOSS_LIMIT", "RECONCILE_MISMATCH",
                                    "RISK_ENGINE_ERROR"])
def test_latched_blocks_need_manual_reset(reason: str) -> None:
    g = Gate()
    g.set(reason, T0)
    assert g.clear(reason) is False and reason in g.blocks
    assert g.latched == [reason]
    assert g.clear(reason, manual=True) is True and g.entries_allowed


def test_auto_blocks_clear() -> None:
    g = Gate()
    g.set("DATA_STALE", T0)
    assert g.clear("DATA_STALE") is True and g.entries_allowed


def test_since_is_kept_on_repeat_set() -> None:
    g = Gate()
    g.set("DATA_STALE", T0)
    g.set("DATA_STALE", datetime(2026, 9, 21, 15, 0, tzinfo=UTC))
    assert g.blocks["DATA_STALE"] == T0


def test_unknown_reason_rejected() -> None:
    with pytest.raises(ValueError):
        Gate().set("NOPE", T0)


def test_json_is_priority_ordered() -> None:
    g = Gate()
    g.set("PAUSED_BY_USER", T0)
    g.set("CONFIG_ERROR", T0, "bad url")
    out = g.to_json()
    assert [b["reason"] for b in out] == ["CONFIG_ERROR", "PAUSED_BY_USER"]
    assert out[0]["detail"] == "bad url" and out[0]["latched"] is False
