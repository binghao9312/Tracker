"""Async PostgreSQL persistence for aggregated metrics only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, String, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MarketMetricRow(Base):
    __tablename__ = "market_metrics"
    __table_args__ = (Index("ix_market_metrics_lookup", "symbol", "timestamp"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float)
    spread: Mapped[float] = mapped_column(Float)
    bid_depth_0_5: Mapped[float] = mapped_column(Float)
    ask_depth_0_5: Mapped[float] = mapped_column(Float)
    bid_depth_1: Mapped[float] = mapped_column(Float)
    ask_depth_1: Mapped[float] = mapped_column(Float)
    bid_depth_2: Mapped[float] = mapped_column(Float)
    ask_depth_2: Mapped[float] = mapped_column(Float)
    bid_depth_5: Mapped[float] = mapped_column(Float)
    ask_depth_5: Mapped[float] = mapped_column(Float)
    buy_impact_10k: Mapped[float | None] = mapped_column(Float)
    sell_impact_10k: Mapped[float | None] = mapped_column(Float)
    buy_impact_50k: Mapped[float | None] = mapped_column(Float)
    sell_impact_50k: Mapped[float | None] = mapped_column(Float)
    obi: Mapped[float] = mapped_column(Float)


class FlowMetricRow(Base):
    __tablename__ = "flow_metrics"
    __table_args__ = (Index("ix_flow_metrics_lookup", "symbol", "timestamp"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(8))
    buy_volume_1m: Mapped[float] = mapped_column(Float)
    sell_volume_1m: Mapped[float] = mapped_column(Float)
    buy_volume_5m: Mapped[float] = mapped_column(Float)
    sell_volume_5m: Mapped[float] = mapped_column(Float)
    cvd_1m: Mapped[float] = mapped_column(Float)
    cvd_5m: Mapped[float] = mapped_column(Float)
    buy_pressure_1m: Mapped[float | None] = mapped_column(Float)
    buy_pressure_5m: Mapped[float | None] = mapped_column(Float)
    sell_pressure_1m: Mapped[float | None] = mapped_column(Float)
    sell_pressure_5m: Mapped[float | None] = mapped_column(Float)


class DerivativeMetricRow(Base):
    __tablename__ = "derivative_metrics"
    __table_args__ = (Index("ix_derivative_metrics_lookup", "symbol", "timestamp"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    open_interest: Mapped[float] = mapped_column(Float)
    open_interest_usd: Mapped[float] = mapped_column(Float)
    oi_change_5m: Mapped[float | None] = mapped_column(Float)
    oi_change_15m: Mapped[float | None] = mapped_column(Float)
    oi_change_1h: Mapped[float | None] = mapped_column(Float)
    funding_rate: Mapped[float | None] = mapped_column(Float)



class PaperTradeRow(Base):
    __tablename__ = "paper_trades"
    __table_args__ = (
        Index("ix_paper_trades_symbol_opened", "symbol", "opened_at"),
        Index(
            "uq_paper_trades_open_symbol",
            "symbol",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
            sqlite_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    market: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(12), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notional_usdt: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float)
    entry_fee: Mapped[float] = mapped_column(Float)
    exit_fee: Mapped[float | None] = mapped_column(Float)
    gross_pnl: Mapped[float | None] = mapped_column(Float)
    net_pnl: Mapped[float | None] = mapped_column(Float)
    return_pct: Mapped[float | None] = mapped_column(Float)
    entry_activity_score: Mapped[float] = mapped_column(Float)
    entry_liquidity_fragility: Mapped[float | None] = mapped_column(Float)
    entry_trade_bias: Mapped[str] = mapped_column(String(8))
    entry_move_type: Mapped[str | None] = mapped_column(String(32))
    entry_cross_exchange_state: Mapped[str | None] = mapped_column(String(32))
    entry_oi_change_5m: Mapped[float | None] = mapped_column(Float)
    entry_funding: Mapped[float | None] = mapped_column(Float)
    entry_buy_pressure_1m: Mapped[float | None] = mapped_column(Float)
    entry_buy_pressure_5m: Mapped[float | None] = mapped_column(Float)
    entry_sell_pressure_1m: Mapped[float | None] = mapped_column(Float)
    entry_sell_pressure_5m: Mapped[float | None] = mapped_column(Float)
    entry_spot_cvd_5m: Mapped[float | None] = mapped_column(Float)
    entry_perp_cvd_5m: Mapped[float | None] = mapped_column(Float)
    max_favorable_excursion_pct: Mapped[float] = mapped_column(Float, default=0.0)
    max_adverse_excursion_pct: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    holding_seconds: Mapped[float | None] = mapped_column(Float)
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    signal_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    exit_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class PaperTradeEventRow(Base):
    __tablename__ = "paper_trade_events"
    __table_args__ = (Index("ix_paper_trade_events_trade_time", "trade_id", "timestamp"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(64))
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("paper_trades.id", ondelete="SET NULL"), nullable=True
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)


def build_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_trades_open_symbol "
                "ON paper_trades (symbol) WHERE status = 'OPEN'"
            )
        )


def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
