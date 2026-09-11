"""add signal_metrics table

Revision ID: 0002_add_signal_metrics
Revises: 0001_baseline_schema
Create Date: 2026-09-11 12:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_add_signal_metrics"
down_revision: str | None = "0001_baseline_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signal_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("activity_score", sa.Float(), nullable=True),
        sa.Column("liquidity_fragility", sa.Float(), nullable=True),
        sa.Column("move_type", sa.String(length=24), nullable=True),
        sa.Column("cross_exchange_state", sa.String(length=24), nullable=True),
        sa.Column("oi_change_5m", sa.Float(), nullable=True),
        sa.Column("oi_change_15m", sa.Float(), nullable=True),
        sa.Column("oi_change_1h", sa.Float(), nullable=True),
        sa.Column("funding_rate", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_signal_metrics_lookup",
        "signal_metrics",
        ["symbol", "timestamp"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_signal_metrics_lookup", table_name="signal_metrics")
    op.drop_table("signal_metrics")
