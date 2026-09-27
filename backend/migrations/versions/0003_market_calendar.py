"""Market calendar table (Phase 2).

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_calendar",
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("open_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("early_close", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("session_date", name=op.f("pk_market_calendar")),
    )
    op.execute("GRANT SELECT ON market_calendar TO intraday_api_role")
    op.execute("GRANT SELECT, INSERT, UPDATE ON market_calendar TO intraday_engine_role")


def downgrade() -> None:
    op.drop_table("market_calendar")
