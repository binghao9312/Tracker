"""Rebuild research features from the metrics the live runtime persisted."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil, floor
from typing import Any

import numpy as np
from sqlalchemy import select

from app.database import DerivativeMetricRow, FlowMetricRow, MarketMetricRow, SignalMetricRow
from app.flow import pressure
from app.models import Exchange, MarketType
from app.scoring import (
    ClassificationThresholds,
    MarketSignal,
    activity_score,
    aggregate_move_type,
    classify_move,
    cross_exchange_state,
)
from app.trade_signal import calculate_trade_signal

BIAS_CODES: Mapping[str, float] = {"LONG": 1.0, "SHORT": -1.0, "NONE": 0.0}

_FEATURE_NAMES = (
    "activity_score",
    "bias",
    "price",
    "liquidity_fragility",
    "confirmed",
    "funding",
    "oi_change_5m",
    "oi_change_15m",
    "oi_change_1h",
)
_MARKETS = tuple(market.value for market in MarketType)
_EXCHANGES = tuple(exchange.value for exchange in Exchange)
_THRESHOLDS = ClassificationThresholds()


@dataclass(frozen=True)
class Panel:
    grid: np.ndarray
    symbols: tuple[str, ...]
    features: Mapping[str, np.ndarray]
    categoricals: Mapping[str, np.ndarray]
    tradable: np.ndarray
    step_seconds: int

    def feature(self, name: str) -> np.ndarray:
        return self.features[name]

    def categorical(self, name: str) -> np.ndarray:
        return self.categoricals[name]

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.grid), len(self.symbols))


def _utc(timestamp: datetime) -> datetime:
    return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)


async def _stream(session: Any, statement: Any) -> Any:
    """Use a server-side cursor in production and chunked iteration in sync test sessions."""
    statement = statement.execution_options(yield_per=256)
    if stream := getattr(session, "stream", None):
        return await stream(statement)
    return await session.execute(statement)


async def _next_row(rows: Any) -> Any | None:
    if hasattr(rows, "__anext__"):
        try:
            return await anext(rows)
        except StopAsyncIteration:
            return None
    return next(rows, None)


async def _advance(
    rows: Any,
    row: Any | None,
    latest: dict[str, dict[Hashable, Any]],
    key: Callable[[Any], Hashable],
    moment: datetime,
) -> Any | None:
    while row is not None and _utc(row.timestamp) <= moment:
        latest.setdefault(row.symbol, {})[key(row)] = row
        row = await _next_row(rows)
    return row


def _fresh(
    rows: Mapping[Hashable, Any], moment: datetime, tolerance_seconds: int
) -> dict[Hashable, Any]:
    return {
        row_key: row
        for row_key, row in rows.items()
        if (moment - _utc(row.timestamp)).total_seconds() <= tolerance_seconds
    }


def _ordered_rows(rows: Mapping[tuple[str, str], Any]) -> dict[tuple[str, str], Any]:
    return {
        (exchange, market): rows[(exchange, market)]
        for exchange in _EXCHANGES
        for market in _MARKETS
        if (exchange, market) in rows
    }


def _pressure_magnitude(buy: float | None, sell: float | None) -> float | None:
    values = [value for value in (buy, sell) if value is not None]
    return max(values) if values else None


def _delta_ratio(buy: float | None, sell: float | None) -> float | None:
    if buy is None or sell is None:
        return None
    total = buy + sell
    return (buy - sell) / total if total else 0.0


def _largest_absolute(values: Sequence[float | None]) -> float | None:
    available = [value for value in values if value is not None]
    return max(available, key=abs) if available else None


def _combined_flow(
    books: Mapping[tuple[str, str], MarketMetricRow],
    flows: Mapping[tuple[str, str], FlowMetricRow],
    market: str,
) -> dict[str, float | None]:
    volume_keys = ("buy_volume_1m", "sell_volume_1m", "buy_volume_5m", "sell_volume_5m")
    combined: dict[str, float | None] = {key: None for key in volume_keys}
    with_book: dict[str, float | None] = {key: None for key in volume_keys}
    for exchange in _EXCHANGES:
        flow = flows.get((exchange, market))
        if flow is None:
            continue
        for key in volume_keys:
            value = getattr(flow, key)
            if value is None:
                continue
            combined[key] = (combined[key] or 0.0) + value
            if (exchange, market) in books:
                with_book[key] = (with_book[key] or 0.0) + value
    ask_depth = sum(
        book.ask_depth_2 for (_, book_market), book in books.items() if book_market == market
    )
    bid_depth = sum(
        book.bid_depth_2 for (_, book_market), book in books.items() if book_market == market
    )
    return {
        "buy_pressure_1m": None
        if with_book["buy_volume_1m"] is None
        else pressure(with_book["buy_volume_1m"], ask_depth),
        "sell_pressure_1m": None
        if with_book["sell_volume_1m"] is None
        else pressure(with_book["sell_volume_1m"], bid_depth),
        "buy_pressure_5m": None
        if with_book["buy_volume_5m"] is None
        else pressure(with_book["buy_volume_5m"], ask_depth),
        "sell_pressure_5m": None
        if with_book["sell_volume_5m"] is None
        else pressure(with_book["sell_volume_5m"], bid_depth),
        "buy_volume_5m": combined["buy_volume_5m"],
        "sell_volume_5m": combined["sell_volume_5m"],
    }


def _preferred_price(books: Mapping[tuple[str, str], MarketMetricRow]) -> float | None:
    for exchange in _EXCHANGES:
        book = books.get((exchange, MarketType.PERP.value))
        if book is not None:
            return book.price
    return next(iter(books.values())).price if books else None


async def build_panel(
    session_factory: Any,
    *,
    start: datetime,
    end: datetime,
    step_seconds: int = 10,
    fresh_tolerance_seconds: int = 30,
    derivative_tolerance_seconds: int = 120,
    symbols: Sequence[str] | None = None,
    raw_columns: Sequence[str] = (),
) -> Panel:
    """Recompute the runtime detail fields on an evenly spaced historical grid."""
    if end < start:
        raise ValueError("end must not precede start")
    if step_seconds <= 0:
        raise ValueError("step_seconds must be positive")
    if fresh_tolerance_seconds < 0 or derivative_tolerance_seconds < 0:
        raise ValueError("freshness tolerances must be nonnegative")
    unknown_columns = [column for column in raw_columns if not hasattr(MarketMetricRow, column)]
    if unknown_columns:
        raise ValueError(f"unknown market metric columns: {', '.join(unknown_columns)}")

    start = _utc(start)
    end = _utc(end)
    lookback = start - timedelta(seconds=max(fresh_tolerance_seconds, derivative_tolerance_seconds))
    requested = set(symbols) if symbols is not None else None

    def in_window(table: Any) -> Any:
        statement = select(table.symbol).where(table.timestamp >= start, table.timestamp <= end)
        return statement.where(table.symbol.in_(requested)) if requested is not None else statement

    first_epoch = ceil(start.timestamp())
    last_epoch = floor(end.timestamp())
    grid = np.arange(first_epoch, last_epoch + 1, step_seconds, dtype=np.int64)

    async with session_factory() as session:
        symbol_statement = in_window(MarketMetricRow).union(
            in_window(FlowMetricRow),
            in_window(DerivativeMetricRow),
            in_window(SignalMetricRow),
        )
        panel_symbols = tuple(sorted((await session.execute(symbol_statement)).scalars()))
        shape = (len(grid), len(panel_symbols))
        features = {name: np.full(shape, np.nan, dtype=np.float32) for name in _FEATURE_NAMES}
        categoricals = {"move_type": np.full(shape, None, dtype=object)}
        tradable = np.zeros(shape, dtype=bool)

        venues = await session.execute(
            select(MarketMetricRow.exchange, MarketMetricRow.market)
            .where(MarketMetricRow.timestamp >= lookback, MarketMetricRow.timestamp <= end)
            .distinct()
        )
        for exchange, market in venues:
            for column in raw_columns:
                features[f"{exchange}_{market}_{column}"] = np.full(shape, np.nan, dtype=np.float32)

        market_columns = (
            MarketMetricRow.timestamp,
            MarketMetricRow.exchange,
            MarketMetricRow.symbol,
            MarketMetricRow.market,
            MarketMetricRow.price,
            MarketMetricRow.bid_depth_2,
            MarketMetricRow.ask_depth_2,
            *(
                getattr(MarketMetricRow, column)
                for column in raw_columns
                if column not in {"price", "bid_depth_2", "ask_depth_2"}
            ),
        )
        market_rows = await _stream(
            session,
            select(*market_columns)
            .where(MarketMetricRow.timestamp >= lookback, MarketMetricRow.timestamp <= end)
            .order_by(MarketMetricRow.timestamp, MarketMetricRow.id),
        )
        flow_rows = await _stream(
            session,
            select(
                FlowMetricRow.timestamp,
                FlowMetricRow.exchange,
                FlowMetricRow.symbol,
                FlowMetricRow.market,
                FlowMetricRow.buy_volume_1m,
                FlowMetricRow.sell_volume_1m,
                FlowMetricRow.buy_volume_5m,
                FlowMetricRow.sell_volume_5m,
                FlowMetricRow.cvd_5m,
                FlowMetricRow.buy_pressure_5m,
                FlowMetricRow.sell_pressure_5m,
            )
            .where(FlowMetricRow.timestamp >= lookback, FlowMetricRow.timestamp <= end)
            .order_by(FlowMetricRow.timestamp, FlowMetricRow.id),
        )
        derivative_rows = await _stream(
            session,
            select(
                DerivativeMetricRow.timestamp,
                DerivativeMetricRow.exchange,
                DerivativeMetricRow.symbol,
                DerivativeMetricRow.oi_change_5m,
                DerivativeMetricRow.oi_change_15m,
                DerivativeMetricRow.oi_change_1h,
                DerivativeMetricRow.funding_rate,
            )
            .where(
                DerivativeMetricRow.timestamp >= lookback,
                DerivativeMetricRow.timestamp <= end,
            )
            .order_by(DerivativeMetricRow.timestamp, DerivativeMetricRow.id),
        )
        signal_rows = await _stream(
            session,
            select(
                SignalMetricRow.timestamp,
                SignalMetricRow.symbol,
                SignalMetricRow.liquidity_fragility,
            )
            .where(SignalMetricRow.timestamp >= lookback, SignalMetricRow.timestamp <= end)
            .order_by(SignalMetricRow.timestamp, SignalMetricRow.id),
        )

        market_head = await _next_row(market_rows)
        flow_head = await _next_row(flow_rows)
        derivative_head = await _next_row(derivative_rows)
        signal_head = await _next_row(signal_rows)
        market_latest: dict[str, dict[Hashable, Any]] = {}
        flow_latest: dict[str, dict[Hashable, Any]] = {}
        derivative_latest: dict[str, dict[Hashable, Any]] = {}
        signal_latest: dict[str, dict[Hashable, Any]] = {}

        for time_index, epoch in enumerate(grid):
            moment = datetime.fromtimestamp(int(epoch), UTC)
            market_head = await _advance(
                market_rows,
                market_head,
                market_latest,
                lambda row: (row.exchange, row.market),
                moment,
            )
            flow_head = await _advance(
                flow_rows,
                flow_head,
                flow_latest,
                lambda row: (row.exchange, row.market),
                moment,
            )
            derivative_head = await _advance(
                derivative_rows,
                derivative_head,
                derivative_latest,
                lambda row: row.exchange,
                moment,
            )
            signal_head = await _advance(
                signal_rows,
                signal_head,
                signal_latest,
                lambda row: "signal",
                moment,
            )
            for symbol_index, symbol in enumerate(panel_symbols):
                books = _ordered_rows(
                    _fresh(market_latest.get(symbol, {}), moment, fresh_tolerance_seconds)
                )
                if not books:
                    continue
                flows = _ordered_rows(
                    _fresh(flow_latest.get(symbol, {}), moment, fresh_tolerance_seconds)
                )
                derivatives = _fresh(
                    derivative_latest.get(symbol, {}), moment, derivative_tolerance_seconds
                )
                signals = _fresh(signal_latest.get(symbol, {}), moment, fresh_tolerance_seconds)
                spot = _combined_flow(books, flows, MarketType.SPOT.value)
                perp = _combined_flow(books, flows, MarketType.PERP.value)

                exchange_signals: list[MarketSignal] = []
                for exchange in _EXCHANGES:
                    derivative = derivatives.get(exchange)
                    if (
                        not any(book_exchange == exchange for book_exchange, _ in books)
                        and derivative is None
                    ):
                        continue
                    spot_flow = flows.get((exchange, MarketType.SPOT.value))
                    perp_flow = flows.get((exchange, MarketType.PERP.value))
                    exchange_signals.append(
                        MarketSignal(
                            exchange,
                            spot_flow.buy_pressure_5m if spot_flow else None,
                            spot_flow.sell_pressure_5m if spot_flow else None,
                            perp_flow.buy_pressure_5m if perp_flow else None,
                            perp_flow.sell_pressure_5m if perp_flow else None,
                            spot_flow.cvd_5m if spot_flow else None,
                            perp_flow.cvd_5m if perp_flow else None,
                            derivative.oi_change_5m if derivative else None,
                        )
                    )
                confirmed = cross_exchange_state(exchange_signals, _THRESHOLDS)
                move_type = aggregate_move_type(
                    classify_move(signal, _THRESHOLDS) for signal in exchange_signals
                )
                oi_change = {
                    window: _largest_absolute(
                        [
                            getattr(derivative, f"oi_change_{window}")
                            for derivative in derivatives.values()
                        ]
                    )
                    for window in ("5m", "15m", "1h")
                }
                funding_values = [
                    derivative.funding_rate
                    for derivative in derivatives.values()
                    if derivative.funding_rate is not None
                ]
                funding = sum(funding_values) / len(funding_values) if funding_values else None
                score = activity_score(
                    oi_change=oi_change["5m"],
                    funding=funding,
                    confirmed=confirmed.value == "CONFIRMED",
                    spot_pressure_1m=_pressure_magnitude(
                        spot["buy_pressure_1m"], spot["sell_pressure_1m"]
                    ),
                    spot_pressure_5m=_pressure_magnitude(
                        spot["buy_pressure_5m"], spot["sell_pressure_5m"]
                    ),
                    spot_cvd_ratio=_delta_ratio(spot["buy_volume_5m"], spot["sell_volume_5m"]),
                    perp_pressure_1m=_pressure_magnitude(
                        perp["buy_pressure_1m"], perp["sell_pressure_1m"]
                    ),
                    perp_pressure_5m=_pressure_magnitude(
                        perp["buy_pressure_5m"], perp["sell_pressure_5m"]
                    ),
                    perp_cvd_ratio=_delta_ratio(perp["buy_volume_5m"], perp["sell_volume_5m"]),
                )
                detail = {
                    "spot": dict(spot),
                    "perp": dict(perp),
                    "buy_pressure_5m": perp["buy_pressure_5m"],
                    "sell_pressure_5m": perp["sell_pressure_5m"],
                    "oi_change_5m": oi_change["5m"],
                    "cross_exchange_state": confirmed.value,
                }
                bias = calculate_trade_signal(detail).bias.value

                features["activity_score"][time_index, symbol_index] = score
                features["bias"][time_index, symbol_index] = BIAS_CODES[bias]
                features["price"][time_index, symbol_index] = _preferred_price(books)
                features["confirmed"][time_index, symbol_index] = float(
                    confirmed.value == "CONFIRMED"
                )
                features["funding"][time_index, symbol_index] = (
                    np.nan if funding is None else funding
                )
                for window, value in oi_change.items():
                    features[f"oi_change_{window}"][time_index, symbol_index] = (
                        np.nan if value is None else value
                    )
                signal = signals.get("signal")
                if signal is not None and signal.liquidity_fragility is not None:
                    features["liquidity_fragility"][time_index, symbol_index] = (
                        signal.liquidity_fragility
                    )
                categoricals["move_type"][time_index, symbol_index] = move_type.value
                tradable[time_index, symbol_index] = any(
                    market == MarketType.PERP.value for _, market in books
                )
                for (exchange, market), book in books.items():
                    for column in raw_columns:
                        feature = features[f"{exchange}_{market}_{column}"]
                        feature[time_index, symbol_index] = getattr(book, column)

    return Panel(
        grid=grid,
        symbols=panel_symbols,
        features=features,
        categoricals=categoricals,
        tradable=tradable,
        step_seconds=step_seconds,
    )
