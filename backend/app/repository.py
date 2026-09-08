"""Database repository for one-second aggregated metric history."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import DerivativeMetricRow, FlowMetricRow, MarketMetricRow


class MetricRepository:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def append_market(self, values: Mapping[str, Any]) -> None:
        await self._append(MarketMetricRow(**values))

    async def append_flow(self, values: Mapping[str, Any]) -> None:
        await self._append(FlowMetricRow(**values))

    async def append_derivative(self, values: Mapping[str, Any]) -> None:
        await self._append(DerivativeMetricRow(**values))

    async def history(self, symbol: str, limit: int = 3_600) -> dict[str, list[dict[str, Any]]]:
        async with self._sessions() as session:
            market = await session.scalars(
                select(MarketMetricRow)
                .where(MarketMetricRow.symbol == symbol)
                .order_by(MarketMetricRow.timestamp.desc())
                .limit(limit)
            )
            flow = await session.scalars(
                select(FlowMetricRow)
                .where(FlowMetricRow.symbol == symbol)
                .order_by(FlowMetricRow.timestamp.desc())
                .limit(limit)
            )
            derivative = await session.scalars(
                select(DerivativeMetricRow)
                .where(DerivativeMetricRow.symbol == symbol)
                .order_by(DerivativeMetricRow.timestamp.desc())
                .limit(limit)
            )
            return {
                "market": [_row_dict(row) for row in reversed(market.all())],
                "flow": [_row_dict(row) for row in reversed(flow.all())],
                "derivative": [_row_dict(row) for row in reversed(derivative.all())],
            }

    async def _append(self, row: MarketMetricRow | FlowMetricRow | DerivativeMetricRow) -> None:
        async with self._sessions.begin() as session:
            session.add(row)


def _row_dict(row: MarketMetricRow | FlowMetricRow | DerivativeMetricRow) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return result
