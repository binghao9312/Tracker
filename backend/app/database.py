"""Async PostgreSQL persistence for aggregated metrics only."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String
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


def build_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
