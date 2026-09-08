"""Database repository for one-second aggregated metric history."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from math import isfinite

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import (
    DerivativeMetricRow,
    FlowMetricRow,
    MarketMetricRow,
    PaperTradeEventRow,
    PaperTradeRow,
)


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

    async def history_range(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
        *,
        exchange: str | None = None,
        market: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        async with self._sessions() as session:
            async def rows(
                row_type: type[MarketMetricRow] | type[FlowMetricRow] | type[DerivativeMetricRow],
                *,
                filter_market: bool,
            ) -> list[dict[str, Any]]:
                conditions = [
                    row_type.symbol == symbol,
                    row_type.timestamp >= start_time,
                    row_type.timestamp <= end_time,
                ]
                if exchange is not None:
                    conditions.append(row_type.exchange == exchange)
                if filter_market and market is not None:
                    conditions.append(row_type.market == market)
                result = await session.scalars(select(row_type).where(*conditions).order_by(row_type.timestamp))
                return [_history_row_dict(row) for row in result.all()]

            return {
                "market": await rows(MarketMetricRow, filter_market=True),
                "flow": await rows(FlowMetricRow, filter_market=True),
                "derivative": await rows(DerivativeMetricRow, filter_market=False),
            }

    async def _append(self, row: MarketMetricRow | FlowMetricRow | DerivativeMetricRow) -> None:
        async with self._sessions.begin() as session:
            session.add(row)



class DuplicateOpenTrade(ValueError):
    """Raised when durable storage already has an OPEN position for a symbol."""


class PaperTradeRepository:
    """Durable local record of simulated perpetual positions and audit events."""

    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def open_trade(self, values: Mapping[str, Any]) -> dict[str, Any]:
        try:
            async with self._sessions.begin() as session:
                row = PaperTradeRow(**values)
                session.add(row)
                await session.flush()
                return _row_dict(row)
        except IntegrityError as error:
            raise DuplicateOpenTrade(str(values["symbol"])) from error

    async def close_trade(self, trade_id: int, values: Mapping[str, Any]) -> dict[str, Any]:
        async with self._sessions.begin() as session:
            row = await session.get(PaperTradeRow, trade_id)
            if row is None:
                raise KeyError(f"paper trade {trade_id} does not exist")
            for key, value in values.items():
                setattr(row, key, value)
            await session.flush()
            return _row_dict(row)

    async def get_open_positions(self) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            result = await session.scalars(
                select(PaperTradeRow)
                .where(PaperTradeRow.status == "OPEN")
                .order_by(PaperTradeRow.opened_at)
            )
            return [_row_dict(row) for row in result.all()]

    async def get_open_position(self, symbol: str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(PaperTradeRow).where(
                    PaperTradeRow.symbol == symbol, PaperTradeRow.status == "OPEN"
                )
            )
            return _row_dict(row) if row is not None else None
    async def get_recent_closed_positions(self, since: datetime) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(PaperTradeRow)
                .where(PaperTradeRow.status == "CLOSED", PaperTradeRow.closed_at >= since)
                .order_by(PaperTradeRow.closed_at.desc())
            )
            return [_row_dict(row) for row in rows.all()]

    async def list_trades(
        self,
        symbol: str | None = None,
        side: str | None = None,
        result: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            statement = select(PaperTradeRow).order_by(PaperTradeRow.opened_at.desc()).limit(limit)
            if symbol:
                statement = statement.where(PaperTradeRow.symbol == symbol.upper())
            if side:
                statement = statement.where(PaperTradeRow.side == side.upper())
            if result == "open":
                statement = statement.where(PaperTradeRow.status == "OPEN")
            elif result == "win":
                statement = statement.where(PaperTradeRow.status == "CLOSED", PaperTradeRow.net_pnl > 0)
            elif result == "loss":
                statement = statement.where(PaperTradeRow.status == "CLOSED", PaperTradeRow.net_pnl < 0)
            rows = await session.scalars(statement)
            return [_row_dict(row) for row in rows.all()]

    async def get_trade(self, trade_id: int) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = await session.get(PaperTradeRow, trade_id)
            return _row_dict(row) if row is not None else None

    async def append_event(
        self,
        *,
        timestamp: datetime,
        symbol: str,
        event_type: str,
        reason: str | None = None,
        trade_id: int | None = None,
        snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._sessions.begin() as session:
            row = PaperTradeEventRow(
                timestamp=timestamp,
                symbol=symbol,
                event_type=event_type,
                reason=reason,
                trade_id=trade_id,
                snapshot=dict(snapshot or {}),
            )
            session.add(row)
            await session.flush()
            return _row_dict(row)

    async def stats(self) -> dict[str, Any]:
        trades = await self.list_trades(limit=100_000)
        closed = [trade for trade in trades if trade["status"] == "CLOSED"]
        wins = [trade for trade in closed if (trade["net_pnl"] or 0) > 0]
        losses = [trade for trade in closed if (trade["net_pnl"] or 0) < 0]
        gross_profit = _finite_sum(trade["net_pnl"] for trade in wins)
        gross_loss = abs(_finite_sum(trade["net_pnl"] for trade in losses))
        return {
            "total_trades": len(closed),
            "open_trades": len(trades) - len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "gross_pnl": _finite_sum(trade["gross_pnl"] or 0 for trade in closed),
            "net_pnl": _finite_sum(trade["net_pnl"] or 0 for trade in closed),
            "average_return": _average(closed, "return_pct"),
            "average_win": _average(wins, "return_pct"),
            "average_loss": _average(losses, "return_pct"),
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "average_holding_seconds": _average(closed, "holding_seconds"),
            "average_mfe": _average(closed, "max_favorable_excursion_pct"),
            "average_mae": _average(closed, "max_adverse_excursion_pct"),
            "breakdowns": {
                "activity_score": _buckets(closed, "entry_activity_score", [(80, 85), (85, 90), (90, 95), (95, 100)]),
                "liquidity_fragility": _buckets(closed, "entry_liquidity_fragility", [(0, 25), (25, 50), (50, 75), (75, 100)]),
                "direction": _groups(closed, "side", ["LONG", "SHORT"]),
                "move_type": _groups(closed, "entry_move_type", ["SPOT_DRIVEN", "LEVERAGE_DRIVEN", "MIXED"]),
                "cross_exchange": _groups(closed, "entry_cross_exchange_state", ["CONFIRMED", "DIVERGENT", "SINGLE_EXCHANGE"]),
            },
        }

def _average(rows: list[dict[str, Any]], key: str) -> float:
    values = [_finite_number(row[key]) for row in rows if row.get(key) is not None]
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else 0.0


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [row for row in rows if (_finite_number(row.get("net_pnl")) or 0) > 0]
    return {
        "trades": len(rows),
        "win_rate": len(wins) / len(rows) if rows else 0.0,
        "average_return": _average(rows, "return_pct"),
        "net_pnl": _finite_sum(row.get("net_pnl") or 0 for row in rows),
    }


def _buckets(
    rows: list[dict[str, Any]], key: str, ranges: list[tuple[int, int]]
) -> dict[str, dict[str, Any]]:
    return {
        f"{lower}-{upper}": _summary(
            [
                row
                for row in rows
                if (value := _bucket_number(row.get(key))) is not None
                and (
                    Decimal(lower) <= value < Decimal(upper)
                    if index < len(ranges) - 1
                    else Decimal(lower) <= value <= Decimal(upper)
                )
            ]
        )
        for index, (lower, upper) in enumerate(ranges)
    }


def _groups(
    rows: list[dict[str, Any]], key: str, values: list[str]
) -> dict[str, dict[str, Any]]:
    return {value: _summary([row for row in rows if row.get(key) == value]) for value in values}


def _bucket_number(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _finite_sum(values: object) -> float:
    return sum(value for item in values if (value := _finite_number(item)) is not None)


def _row_dict(
    row: MarketMetricRow | FlowMetricRow | DerivativeMetricRow | PaperTradeRow | PaperTradeEventRow,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return result


def _history_row_dict(row: MarketMetricRow | FlowMetricRow | DerivativeMetricRow) -> dict[str, Any]:
    result = _row_dict(row)
    timestamp = getattr(row, "timestamp")
    result["timestamp"] = int(timestamp.timestamp() * 1_000)
    return result
