"""Reconstruct runtime research features from persisted metric rows."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

import numpy as np
from sqlalchemy import Select, select

from app.database import (
    DerivativeMetricRow,
    FlowMetricRow,
    MarketMetricRow,
    SignalMetricRow,
)
from app.flow import pressure
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
_EXCHANGES = ("binance", "okx")
_MARKETS = ("spot", "perp")
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


class _Timeline:
    """Latest-row lookup for monotonically increasing grid timestamps."""

    def __init__(self, rows: Sequence[Any], key: Callable[[Any], tuple[str, ...]]) -> None:
        self._entries = sorted(
            (
                _timestamp(row.timestamp),
                getattr(row, "id", 0),
                row,
            )
            for row in rows
        )
        self._key = key
        self._index = 0
        self._current: dict[tuple[str, ...], tuple[datetime, Any]] = {}

    def at(self, moment: datetime, tolerance: int) -> dict[tuple[str, ...], Any]:
        while self._index < len(self._entries) and self._entries[self._index][0] <= moment:
            timestamp, _, row = self._entries[self._index]
            self._current[self._key(row)] = (timestamp, row)
            self._index += 1
        cutoff = moment - timedelta(seconds=tolerance)
        return {
            key: row
            for key, (timestamp, row) in self._current.items()
            if timestamp >= cutoff
        }


def _timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _latest_timestamp(rows: Sequence[Any]) -> datetime | None:
    if not rows:
        return None
    return max(_timestamp(row.timestamp) for row in rows)


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
    books: Mapping[tuple[str, str], Any],
    flows: Mapping[tuple[str, str], Any],
    market: str,
) -> dict[str, float | None]:
    volume_keys = (
        "buy_volume_1m",
        "sell_volume_1m",
        "buy_volume_5m",
        "sell_volume_5m",
    )
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
            combined[key] = (combined[key] or 0.0) + float(value)
            if (exchange, market) in books:
                with_book[key] = (with_book[key] or 0.0) + float(value)

    ask_depth = sum(
        float(book.ask_depth_2) for (exchange, book_market), book in books.items() if book_market == market
    )
    bid_depth = sum(
        float(book.bid_depth_2) for (exchange, book_market), book in books.items() if book_market == market
    )
    combined["buy_pressure_1m"] = (
        None
        if with_book["buy_volume_1m"] is None
        else pressure(with_book["buy_volume_1m"], ask_depth)
    )
    combined["sell_pressure_1m"] = (
        None
        if with_book["sell_volume_1m"] is None
        else pressure(with_book["sell_volume_1m"], bid_depth)
    )
    combined["buy_pressure_5m"] = (
        None
        if with_book["buy_volume_5m"] is None
        else pressure(with_book["buy_volume_5m"], ask_depth)
    )
    combined["sell_pressure_5m"] = (
        None
        if with_book["sell_volume_5m"] is None
        else pressure(with_book["sell_volume_5m"], bid_depth)
    )
    return combined


def _point(
    books: Mapping[tuple[str, str], Any],
    flows: Mapping[tuple[str, str], Any],
    derivatives: Mapping[str, Any],
    signal: Any | None,
) -> dict[str, Any]:
    spot = _combined_flow(books, flows, "spot")
    perp = _combined_flow(books, flows, "perp")

    signals: list[MarketSignal] = []
    for exchange in _EXCHANGES:
        derivative = derivatives.get(exchange)
        has_book = any(key[0] == exchange for key in books)
        if not has_book and derivative is None:
            continue
        spot_flow = flows.get((exchange, "spot"))
        perp_flow = flows.get((exchange, "perp"))
        signals.append(
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

    confirmed = cross_exchange_state(signals, _THRESHOLDS)
    move_type = aggregate_move_type(classify_move(item, _THRESHOLDS) for item in signals)
    oi_change = {
        window: _largest_absolute(
            [getattr(derivative, f"oi_change_{window}") for derivative in derivatives.values()]
        )
        for window in ("5m", "15m", "1h")
    }
    funding_values = [
        float(derivative.funding_rate)
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
    bias = calculate_trade_signal(detail).bias

    price = None
    for key in (("binance", "perp"), ("okx", "perp")):
        if key in books:
            price = books[key].price
            break
    if price is None and books:
        price = next(iter(books.values())).price

    return {
        "activity_score": score,
        "bias": BIAS_CODES[bias.value],
        "price": price,
        "funding": funding,
        "oi_change_5m": oi_change["5m"],
        "oi_change_15m": oi_change["15m"],
        "oi_change_1h": oi_change["1h"],
        "confirmed": 1.0 if confirmed.value == "CONFIRMED" else 0.0,
        "move_type": move_type.value,
        "tradable": any(key[1] == "perp" for key in books),
        "liquidity_fragility": signal.liquidity_fragility if signal is not None else None,
    }


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
    """Build a regular feature grid using the same aggregations as the live runtime."""
    if step_seconds <= 0:
        raise ValueError("step_seconds must be positive")
    if fresh_tolerance_seconds < 0 or derivative_tolerance_seconds < 0:
        raise ValueError("freshness tolerances must be nonnegative")

    start_utc = _timestamp(start)
    end_utc = _timestamp(end)
    if end_utc < start_utc:
        raise ValueError("end must not precede start")

    start_seconds = start_utc.timestamp()
    end_seconds = end_utc.timestamp()
    first_second = math.ceil(start_seconds)
    last_second = math.floor(end_seconds)
    if first_second <= last_second:
        count = (last_second - first_second) // step_seconds + 1
        grid = first_second + np.arange(count, dtype=np.int64) * step_seconds
    else:
        grid = np.empty(0, dtype=np.int64)

    lookback = max(fresh_tolerance_seconds, derivative_tolerance_seconds)
    query_start = start_utc - timedelta(seconds=lookback)
    statements: tuple[Select[Any], ...] = (
        select(MarketMetricRow)
        .where(MarketMetricRow.timestamp >= query_start, MarketMetricRow.timestamp <= end_utc)
        .order_by(MarketMetricRow.timestamp.asc(), MarketMetricRow.id.asc()),
        select(FlowMetricRow)
        .where(FlowMetricRow.timestamp >= query_start, FlowMetricRow.timestamp <= end_utc)
        .order_by(FlowMetricRow.timestamp.asc(), FlowMetricRow.id.asc()),
        select(DerivativeMetricRow)
        .where(
            DerivativeMetricRow.timestamp >= query_start,
            DerivativeMetricRow.timestamp <= end_utc,
        )
        .order_by(DerivativeMetricRow.timestamp.asc(), DerivativeMetricRow.id.asc()),
        select(SignalMetricRow)
        .where(SignalMetricRow.timestamp >= query_start, SignalMetricRow.timestamp <= end_utc)
        .order_by(SignalMetricRow.timestamp.asc(), SignalMetricRow.id.asc()),
    )
    async with session_factory() as session:
        results = [await session.execute(statement) for statement in statements]
        market_rows = list(results[0].scalars().all())
        flow_rows = list(results[1].scalars().all())
        derivative_rows = list(results[2].scalars().all())
        signal_rows = list(results[3].scalars().all())

    requested = None if symbols is None else {str(symbol) for symbol in symbols}
    market_rows = [
        row
        for row in market_rows
        if (requested is None or row.symbol in requested)
    ]
    flow_rows = [row for row in flow_rows if requested is None or row.symbol in requested]
    derivative_rows = [
        row for row in derivative_rows if requested is None or row.symbol in requested
    ]
    signal_rows = [row for row in signal_rows if requested is None or row.symbol in requested]

    panel_symbols = tuple(
        sorted(
            {
                row.symbol
                for row in market_rows
                if start_utc <= _timestamp(row.timestamp) <= end_utc
            }
        )
    )

    market_timeline = _Timeline(market_rows, lambda row: (row.symbol, row.exchange, row.market))
    flow_timeline = _Timeline(flow_rows, lambda row: (row.symbol, row.exchange, row.market))
    derivative_timeline = _Timeline(derivative_rows, lambda row: (row.symbol, row.exchange))
    signal_timeline = _Timeline(signal_rows, lambda row: (row.symbol,))

    time_count = len(grid)
    symbol_count = len(panel_symbols)
    feature_names = (
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
    features: dict[str, np.ndarray] = {
        name: np.full((time_count, symbol_count), np.nan, dtype=np.float32)
        for name in feature_names
    }
    categoricals = {
        "move_type": np.full((time_count, symbol_count), None, dtype=object)
    }
    tradable = np.zeros((time_count, symbol_count), dtype=bool)
    raw_features = {
        f"{exchange}_{market}_{column}": np.full(
            (time_count, symbol_count), np.nan, dtype=np.float32
        )
        for column in raw_columns
        for exchange in _EXCHANGES
        for market in _MARKETS
    }
    features.update(raw_features)

    for time_index, epoch_seconds in enumerate(grid):
        moment = datetime.fromtimestamp(int(epoch_seconds), UTC)
        current_books = market_timeline.at(moment, fresh_tolerance_seconds)
        current_flows = flow_timeline.at(moment, fresh_tolerance_seconds)
        current_derivatives = derivative_timeline.at(moment, derivative_tolerance_seconds)
        current_signals = signal_timeline.at(moment, fresh_tolerance_seconds)
        for symbol_index, symbol in enumerate(panel_symbols):
            books = {
                (exchange, market): row
                for (row_symbol, exchange, market), row in current_books.items()
                if row_symbol == symbol
            }
            if not books:
                continue
            flows = {
                (exchange, market): row
                for (row_symbol, exchange, market), row in current_flows.items()
                if row_symbol == symbol
            }
            derivatives = {
                exchange: row
                for (row_symbol, exchange), row in current_derivatives.items()
                if row_symbol == symbol
            }
            signal = current_signals.get((symbol,))
            point = _point(books, flows, derivatives, signal)
            for name in feature_names:
                value = point[name]
                if value is not None:
                    features[name][time_index, symbol_index] = float(value)
            categoricals["move_type"][time_index, symbol_index] = point["move_type"]
            tradable[time_index, symbol_index] = point["tradable"]
            for column in raw_columns:
                for exchange in _EXCHANGES:
                    for market in _MARKETS:
                        row = books.get((exchange, market))
                        value = getattr(row, column) if row is not None else None
                        if value is not None:
                            features[f"{exchange}_{market}_{column}"][
                                time_index, symbol_index
                            ] = float(value)

    return Panel(
        grid=grid,
        symbols=panel_symbols,
        features=features,
        categoricals=categoricals,
        tradable=tradable,
        step_seconds=step_seconds,
    )
