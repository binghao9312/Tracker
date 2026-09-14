"""Shared scaffolding for research-module tests: an in-memory DB and row builders.

The research modules read the same tables the live runtime writes, so their tests
need a database rather than a fixture object. SQLite in memory is enough -- the
queries are ordinary filtered selects -- but the repository interface is async and
SQLAlchemy's SQLite driver here is sync, hence the shim below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base

EPOCH = datetime(2026, 9, 14, 0, 0, 0, tzinfo=UTC)


class SyncToAsyncSession:
    """Presents a sync SQLAlchemy session through the async interface app code uses."""

    def __init__(self, sync_session: Any) -> None:
        self.session = sync_session

    async def __aenter__(self) -> SyncToAsyncSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.session.close()

    async def execute(self, statement: object, params: object = None) -> Any:
        return self.session.execute(statement, params)

    async def scalars(self, statement: object, params: object = None) -> Any:
        return self.session.scalars(statement, params)


class SyncToAsyncSessionFactory:
    def __init__(self, sync_sessionmaker: Any) -> None:
        self._factory = sync_sessionmaker

    def __call__(self) -> SyncToAsyncSession:
        return SyncToAsyncSession(self._factory())


def memory_session_factory() -> SyncToAsyncSessionFactory:
    """A fresh in-memory database with the production schema already created."""
    # A shared static pool keeps every connection on the same in-memory database;
    # the default pool would hand each connection its own empty one.
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return SyncToAsyncSessionFactory(sessionmaker(engine, expire_on_commit=False))


def at(seconds: float) -> datetime:
    """A timestamp ``seconds`` after the fixed test epoch."""
    return EPOCH + timedelta(seconds=seconds)


def insert(factory: SyncToAsyncSessionFactory, table: str, rows: list[dict[str, Any]]) -> None:
    """Insert raw rows into one of the metric tables."""
    if not rows:
        return
    session = factory._factory()
    try:
        session.execute(Base.metadata.tables[table].insert(), rows)
        session.commit()
    finally:
        session.close()


def market_row(
    *,
    seconds: float,
    exchange: str,
    symbol: str,
    market: str,
    price: float = 100.0,
    ask_depth_2: float = 1_000.0,
    bid_depth_2: float = 1_000.0,
    spread: float = 0.01,
    obi: float = 0.0,
) -> dict[str, Any]:
    """A market_metrics row. Depth bands other than 2% are filled with plausible values.

    Only ``ask_depth_2``/``bid_depth_2`` feed the pressure calculation the panel has to
    reproduce, so they are the ones worth varying in a test.
    """
    return {
        "timestamp": at(seconds),
        "exchange": exchange,
        "symbol": symbol,
        "market": market,
        "price": price,
        "spread": spread,
        "bid_depth_0_5": bid_depth_2 * 0.25,
        "ask_depth_0_5": ask_depth_2 * 0.25,
        "bid_depth_1": bid_depth_2 * 0.5,
        "ask_depth_1": ask_depth_2 * 0.5,
        "bid_depth_2": bid_depth_2,
        "ask_depth_2": ask_depth_2,
        "bid_depth_5": bid_depth_2 * 2.5,
        "ask_depth_5": ask_depth_2 * 2.5,
        "buy_impact_10k": 0.02,
        "sell_impact_10k": 0.02,
        "buy_impact_50k": 0.09,
        "sell_impact_50k": 0.09,
        "obi": obi,
    }


def flow_row(
    *,
    seconds: float,
    exchange: str,
    symbol: str,
    market: str,
    buy_volume_1m: float = 0.0,
    sell_volume_1m: float = 0.0,
    buy_volume_5m: float = 0.0,
    sell_volume_5m: float = 0.0,
    cvd_1m: float | None = None,
    cvd_5m: float | None = None,
    buy_pressure_1m: float | None = None,
    buy_pressure_5m: float | None = None,
    sell_pressure_1m: float | None = None,
    sell_pressure_5m: float | None = None,
) -> dict[str, Any]:
    """A flow_metrics row.

    The per-exchange ``*_pressure_5m`` and ``cvd_5m`` columns are what
    ``cross_exchange_state`` and ``classify_move`` read, and they are stored as the
    runtime computed them per exchange -- distinct from the cross-exchange pressures
    the panel recomputes from volumes and depth. Defaulting cvd to the volume delta
    keeps rows self-consistent unless a test overrides it.
    """
    return {
        "timestamp": at(seconds),
        "exchange": exchange,
        "symbol": symbol,
        "market": market,
        "buy_volume_1m": buy_volume_1m,
        "sell_volume_1m": sell_volume_1m,
        "buy_volume_5m": buy_volume_5m,
        "sell_volume_5m": sell_volume_5m,
        "cvd_1m": buy_volume_1m - sell_volume_1m if cvd_1m is None else cvd_1m,
        "cvd_5m": buy_volume_5m - sell_volume_5m if cvd_5m is None else cvd_5m,
        "buy_pressure_1m": buy_pressure_1m,
        "buy_pressure_5m": buy_pressure_5m,
        "sell_pressure_1m": sell_pressure_1m,
        "sell_pressure_5m": sell_pressure_5m,
    }


def derivative_row(
    *,
    seconds: float,
    exchange: str,
    symbol: str,
    open_interest: float = 1_000_000.0,
    oi_change_5m: float | None = None,
    oi_change_15m: float | None = None,
    oi_change_1h: float | None = None,
    funding_rate: float | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": at(seconds),
        "exchange": exchange,
        "symbol": symbol,
        "open_interest": open_interest,
        "open_interest_usd": open_interest * 100.0,
        "oi_change_5m": oi_change_5m,
        "oi_change_15m": oi_change_15m,
        "oi_change_1h": oi_change_1h,
        "funding_rate": funding_rate,
    }


def signal_row(
    *,
    seconds: float,
    symbol: str,
    price: float | None = None,
    activity_score: float | None = None,
    liquidity_fragility: float | None = None,
    move_type: str | None = None,
    cross_exchange_state: str | None = None,
    oi_change_5m: float | None = None,
    oi_change_15m: float | None = None,
    oi_change_1h: float | None = None,
    funding_rate: float | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": at(seconds),
        "symbol": symbol,
        "price": price,
        "activity_score": activity_score,
        "liquidity_fragility": liquidity_fragility,
        "move_type": move_type,
        "cross_exchange_state": cross_exchange_state,
        "oi_change_5m": oi_change_5m,
        "oi_change_15m": oi_change_15m,
        "oi_change_1h": oi_change_1h,
        "funding_rate": funding_rate,
    }
