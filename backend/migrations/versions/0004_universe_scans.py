"""Universe scans: scan grouping, metrics, feed; SCAN engine command (Phase 3).

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_COMMANDS = ("START", "PAUSE", "EMERGENCY_STOP", "CLOSE_ALL", "CLOSE_POSITION",
                "RESET_DAILY_LOCK", "RESET_LATCH", "RECONCILE_NOW", "BACKFILL")
NEW_COMMANDS = (*OLD_COMMANDS, "SCAN")


def _command_check(values: tuple[str, ...]) -> str:
    return "command IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column("universe_scans", sa.Column("scan_id", postgresql.UUID(as_uuid=True),
                                              nullable=True))
    op.execute("UPDATE universe_scans SET scan_id = gen_random_uuid() WHERE scan_id IS NULL")
    op.alter_column("universe_scans", "scan_id", nullable=False)
    op.add_column("universe_scans", sa.Column("metrics", postgresql.JSONB(), nullable=False,
                                              server_default="{}"))
    op.add_column("universe_scans", sa.Column("feed", sa.String(8), nullable=False,
                                              server_default="iex"))
    op.create_index(op.f("ix_universe_scans_scan_id"), "universe_scans", ["scan_id"])
    op.create_index("ix_universe_scans_ts_desc", "universe_scans", [sa.text("ts DESC")])

    op.drop_constraint(op.f("ck_engine_commands_command_valid"), "engine_commands", type_="check")
    op.create_check_constraint("command_valid", "engine_commands", _command_check(NEW_COMMANDS))


def downgrade() -> None:
    op.execute("DELETE FROM engine_commands WHERE command = 'SCAN'")
    op.drop_constraint(op.f("ck_engine_commands_command_valid"), "engine_commands", type_="check")
    op.create_check_constraint("command_valid", "engine_commands", _command_check(OLD_COMMANDS))
    op.drop_index("ix_universe_scans_ts_desc", table_name="universe_scans")
    op.drop_index(op.f("ix_universe_scans_scan_id"), table_name="universe_scans")
    op.drop_column("universe_scans", "feed")
    op.drop_column("universe_scans", "metrics")
    op.drop_column("universe_scans", "scan_id")
