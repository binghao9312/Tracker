"""baseline schema

Revision ID: 0001_baseline_schema
Revises:
Create Date: 2026-09-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("market_metrics"):
        # The 5 baseline tables already exist; stamping baseline without recreating.
        return

    op.create_table(
        "market_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("spread", sa.Float(), nullable=False),
        sa.Column("bid_depth_0_5", sa.Float(), nullable=False),
        sa.Column("ask_depth_0_5", sa.Float(), nullable=False),
        sa.Column("bid_depth_1", sa.Float(), nullable=False),
        sa.Column("ask_depth_1", sa.Float(), nullable=False),
        sa.Column("bid_depth_2", sa.Float(), nullable=False),
        sa.Column("ask_depth_2", sa.Float(), nullable=False),
        sa.Column("bid_depth_5", sa.Float(), nullable=False),
        sa.Column("ask_depth_5", sa.Float(), nullable=False),
        sa.Column("buy_impact_10k", sa.Float(), nullable=True),
        sa.Column("sell_impact_10k", sa.Float(), nullable=True),
        sa.Column("buy_impact_50k", sa.Float(), nullable=True),
        sa.Column("sell_impact_50k", sa.Float(), nullable=True),
        sa.Column("obi", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_market_metrics_lookup", "market_metrics", ["symbol", "timestamp"], unique=False
    )
    op.create_index("ix_market_metrics_timestamp", "market_metrics", ["timestamp"], unique=False)

    op.create_table(
        "flow_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("buy_volume_1m", sa.Float(), nullable=False),
        sa.Column("sell_volume_1m", sa.Float(), nullable=False),
        sa.Column("buy_volume_5m", sa.Float(), nullable=False),
        sa.Column("sell_volume_5m", sa.Float(), nullable=False),
        sa.Column("cvd_1m", sa.Float(), nullable=False),
        sa.Column("cvd_5m", sa.Float(), nullable=False),
        sa.Column("buy_pressure_1m", sa.Float(), nullable=True),
        sa.Column("buy_pressure_5m", sa.Float(), nullable=True),
        sa.Column("sell_pressure_1m", sa.Float(), nullable=True),
        sa.Column("sell_pressure_5m", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_flow_metrics_lookup", "flow_metrics", ["symbol", "timestamp"], unique=False)
    op.create_index("ix_flow_metrics_timestamp", "flow_metrics", ["timestamp"], unique=False)

    op.create_table(
        "derivative_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("open_interest", sa.Float(), nullable=False),
        sa.Column("open_interest_usd", sa.Float(), nullable=False),
        sa.Column("oi_change_5m", sa.Float(), nullable=True),
        sa.Column("oi_change_15m", sa.Float(), nullable=True),
        sa.Column("oi_change_1h", sa.Float(), nullable=True),
        sa.Column("funding_rate", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_derivative_metrics_lookup", "derivative_metrics", ["symbol", "timestamp"], unique=False
    )
    op.create_index(
        "ix_derivative_metrics_timestamp", "derivative_metrics", ["timestamp"], unique=False
    )

    op.create_table(
        "paper_trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notional_usdt", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("entry_fee", sa.Float(), nullable=False),
        sa.Column("exit_fee", sa.Float(), nullable=True),
        sa.Column("gross_pnl", sa.Float(), nullable=True),
        sa.Column("net_pnl", sa.Float(), nullable=True),
        sa.Column("return_pct", sa.Float(), nullable=True),
        sa.Column("entry_activity_score", sa.Float(), nullable=False),
        sa.Column("entry_liquidity_fragility", sa.Float(), nullable=True),
        sa.Column("entry_trade_bias", sa.String(length=8), nullable=False),
        sa.Column("entry_move_type", sa.String(length=32), nullable=True),
        sa.Column("entry_cross_exchange_state", sa.String(length=32), nullable=True),
        sa.Column("entry_oi_change_5m", sa.Float(), nullable=True),
        sa.Column("entry_funding", sa.Float(), nullable=True),
        sa.Column("entry_buy_pressure_1m", sa.Float(), nullable=True),
        sa.Column("entry_buy_pressure_5m", sa.Float(), nullable=True),
        sa.Column("entry_sell_pressure_1m", sa.Float(), nullable=True),
        sa.Column("entry_sell_pressure_5m", sa.Float(), nullable=True),
        sa.Column("entry_spot_cvd_5m", sa.Float(), nullable=True),
        sa.Column("entry_perp_cvd_5m", sa.Float(), nullable=True),
        sa.Column("max_favorable_excursion_pct", sa.Float(), nullable=False),
        sa.Column("max_adverse_excursion_pct", sa.Float(), nullable=False),
        sa.Column("exit_reason", sa.String(length=32), nullable=True),
        sa.Column("holding_seconds", sa.Float(), nullable=True),
        sa.Column("settings_snapshot", sa.JSON(), nullable=False),
        sa.Column("signal_snapshot", sa.JSON(), nullable=False),
        sa.Column("exit_snapshot", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_trades_status", "paper_trades", ["status"], unique=False)
    op.create_index("ix_paper_trades_symbol", "paper_trades", ["symbol"], unique=False)
    op.create_index(
        "ix_paper_trades_symbol_opened", "paper_trades", ["symbol", "opened_at"], unique=False
    )
    op.create_index(
        "uq_paper_trades_open_symbol",
        "paper_trades",
        ["symbol"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    op.create_table(
        "paper_trade_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("trade_id", sa.Integer(), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["trade_id"], ["paper_trades.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_trade_events_symbol", "paper_trade_events", ["symbol"], unique=False)
    op.create_index(
        "ix_paper_trade_events_timestamp", "paper_trade_events", ["timestamp"], unique=False
    )
    op.create_index(
        "ix_paper_trade_events_trade_time",
        "paper_trade_events",
        ["trade_id", "timestamp"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("paper_trade_events")
    op.drop_table("paper_trades")
    op.drop_table("derivative_metrics")
    op.drop_table("flow_metrics")
    op.drop_table("market_metrics")
