"""Signal notes and proposal block reasons (Phase 4).

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trading_signals", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("ai_decisions", sa.Column("block_reasons", postgresql.ARRAY(sa.String(32)),
                                            nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("ai_decisions", "block_reasons")
    op.drop_column("trading_signals", "note")
