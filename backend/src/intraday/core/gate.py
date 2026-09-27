"""Trading gate: block reasons, latches, and display status (docs/design/07 §6).

New entries are allowed only when no block is active. The display status is
the SPEC §20 status of the highest-priority active block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

DisplayStatus = Literal["RUNNING", "PAUSED", "DAILY LOSS LIMIT", "MARKET CLOSED", "DATA ERROR",
                        "BROKER ERROR", "EMERGENCY STOP"]


@dataclass(frozen=True)
class BlockSpec:
    reason: str
    display: DisplayStatus
    latched: bool
    description: str


# Ordered by priority (highest first).
BLOCKS: tuple[BlockSpec, ...] = (
    BlockSpec("CONFIG_ERROR", "EMERGENCY STOP", False,
              "Configuration is unsafe or invalid (paper guard or settings); fix and restart."),
    BlockSpec("EMERGENCY_STOP", "EMERGENCY STOP", True, "Emergency stop was triggered."),
    BlockSpec("DAILY_LOSS_LIMIT", "DAILY LOSS LIMIT", True, "Daily loss limit reached."),
    BlockSpec("RISK_ENGINE_ERROR", "BROKER ERROR", True, "Risk engine raised an error."),
    BlockSpec("RECONCILE_MISMATCH", "BROKER ERROR", True,
              "Local and broker state disagree."),
    BlockSpec("RECONCILING", "BROKER ERROR", False, "Reconciling with the broker."),
    BlockSpec("BROKER_DOWN", "BROKER ERROR", False, "Broker API unavailable."),
    BlockSpec("NO_BROKER", "BROKER ERROR", False,
              "No broker: Alpaca paper keys are missing or the broker could not be built."),
    BlockSpec("DB_DOWN", "DATA ERROR", False, "Database unavailable."),
    BlockSpec("DATA_STALE", "DATA ERROR", False, "Market data is stale."),
    BlockSpec("AI_UNAVAILABLE", "DATA ERROR", False, "AI provider unavailable (VETO mode)."),
    BlockSpec("PAUSED_BY_USER", "PAUSED", False, "Trading is paused."),
    BlockSpec("OUTSIDE_ENTRY_WINDOW", "MARKET CLOSED", False, "Outside the entry window."),
    BlockSpec("REGIME_BLOCKED", "RUNNING", False, "Market regime policy blocks entries."),
)
BLOCK_SPECS: dict[str, BlockSpec] = {b.reason: b for b in BLOCKS}
_PRIORITY = {b.reason: i for i, b in enumerate(BLOCKS)}
LATCHABLE = frozenset(b.reason for b in BLOCKS if b.latched)


@dataclass
class Gate:
    """Mutable gate owned by the engine process."""

    blocks: dict[str, datetime] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)

    def set(self, reason: str, since: datetime, detail: str | None = None) -> None:
        if reason not in BLOCK_SPECS:
            raise ValueError(f"unknown block reason {reason!r}")
        self.blocks.setdefault(reason, since)
        if detail:
            self.details[reason] = detail

    def clear(self, reason: str, *, manual: bool = False) -> bool:
        """Clear a block. Latched blocks only clear on a manual reset."""
        if reason not in self.blocks:
            return False
        if BLOCK_SPECS[reason].latched and not manual:
            return False
        del self.blocks[reason]
        self.details.pop(reason, None)
        return True

    @property
    def entries_allowed(self) -> bool:
        return not self.blocks

    @property
    def latched(self) -> list[str]:
        return sorted((r for r in self.blocks if r in LATCHABLE), key=_PRIORITY.__getitem__)

    def display_status(self) -> DisplayStatus:
        if not self.blocks:
            return "RUNNING"
        top = min(self.blocks, key=_PRIORITY.__getitem__)
        return BLOCK_SPECS[top].display

    def to_json(self) -> list[dict[str, Any]]:
        out = []
        for reason in sorted(self.blocks, key=_PRIORITY.__getitem__):
            spec = BLOCK_SPECS[reason]
            out.append({"reason": reason, "since": self.blocks[reason].isoformat(),
                        "latched": spec.latched,
                        "detail": self.details.get(reason, spec.description)})
        return out
