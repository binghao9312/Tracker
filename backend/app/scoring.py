"""Transparent cross-market classification and separate fragility/activity scores."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from statistics import fmean

import yaml

from app.liquidity import LiquidityMetrics


class MoveType(StrEnum):
    SPOT_DRIVEN = "SPOT_DRIVEN"
    LEVERAGE_DRIVEN = "LEVERAGE_DRIVEN"
    MIXED = "MIXED"
    NEUTRAL = "NEUTRAL"


class CrossExchangeState(StrEnum):
    CONFIRMED = "CONFIRMED"
    DIVERGENT = "DIVERGENT"
    SINGLE_EXCHANGE = "SINGLE_EXCHANGE"


@dataclass(frozen=True)
class MarketSignal:
    exchange: str
    spot_buy_pressure: float | None
    spot_sell_pressure: float | None
    perp_buy_pressure: float | None
    perp_sell_pressure: float | None
    spot_cvd: float | None
    perp_cvd: float | None
    oi_change: float | None


class _Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    NONE = "NONE"


@dataclass(frozen=True)
class ClassificationThresholds:
    pressure: float = 2.0
    cvd: float = 0.0
    oi_change: float = 0.05
    allow_missing_cvd: bool = False

    def __post_init__(self) -> None:
        for field in ("pressure", "cvd", "oi_change"):
            object.__setattr__(self, field, _nonnegative_float(field, getattr(self, field)))
        if not isinstance(self.allow_missing_cvd, bool):
            raise ValueError("allow_missing_cvd must be a boolean")


def load_classification_thresholds(path: Path) -> ClassificationThresholds:
    """Load validated classification thresholds from the scoring configuration."""
    values = _scoring_section(path, "classification")
    required = {"pressure", "cvd", "oi_change"}
    missing = required - values.keys()
    if missing:
        raise ValueError(f"classification configuration is missing: {', '.join(sorted(missing))}")
    try:
        return ClassificationThresholds(**dict(values))
    except TypeError as exc:
        raise ValueError("classification configuration has unsupported values") from exc


def _scoring_section(path: Path, section: str) -> Mapping[str, object]:
    with path.open(encoding="utf-8") as file:
        configured = yaml.safe_load(file)
    if not isinstance(configured, Mapping):
        raise ValueError("scoring configuration must be a mapping")
    values = configured.get(section)
    if not isinstance(values, Mapping):
        raise ValueError(f"{section} configuration must be a mapping")
    return values


def _nonnegative_float(field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite nonnegative number")
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return number


def classify_move(signal: MarketSignal, thresholds: ClassificationThresholds) -> MoveType:
    spot_direction = _market_direction(
        signal.spot_buy_pressure, signal.spot_sell_pressure, signal.spot_cvd, thresholds
    )
    perp_direction = _market_direction(
        signal.perp_buy_pressure, signal.perp_sell_pressure, signal.perp_cvd, thresholds
    )
    perp_active = perp_direction is not _Direction.NONE and _above(
        signal.oi_change, thresholds.oi_change
    )
    if spot_direction is not _Direction.NONE and perp_active:
        return MoveType.MIXED if spot_direction is perp_direction else MoveType.NEUTRAL
    if spot_direction is not _Direction.NONE:
        return MoveType.SPOT_DRIVEN
    if perp_active:
        return MoveType.LEVERAGE_DRIVEN
    return MoveType.NEUTRAL


def cross_exchange_state(
    signals: list[MarketSignal], thresholds: ClassificationThresholds
) -> CrossExchangeState:
    if len(signals) < 2:
        return CrossExchangeState.SINGLE_EXCHANGE
    directions = [_exchange_direction(signal, thresholds) for signal in signals]
    if directions[0] is not _Direction.NONE and all(
        direction is directions[0] for direction in directions[1:]
    ):
        return CrossExchangeState.CONFIRMED
    return CrossExchangeState.DIVERGENT


def exchange_directions(
    signals: Iterable[MarketSignal], thresholds: ClassificationThresholds
) -> dict[str, str]:
    """Return each exchange direction used for confirmation and dashboard evidence."""
    return {signal.exchange: _exchange_direction(signal, thresholds).value for signal in signals}


def aggregate_move_type(move_types: Iterable[MoveType]) -> MoveType:
    """Combine exchange classifications without relying on exchange or enum order."""
    non_neutral = {move_type for move_type in move_types if move_type is not MoveType.NEUTRAL}
    if not non_neutral:
        return MoveType.NEUTRAL
    return non_neutral.pop() if len(non_neutral) == 1 else MoveType.MIXED


def _exchange_direction(signal: MarketSignal, thresholds: ClassificationThresholds) -> _Direction:
    directions = {
        _market_direction(
            signal.spot_buy_pressure, signal.spot_sell_pressure, signal.spot_cvd, thresholds
        ),
        _market_direction(
            signal.perp_buy_pressure, signal.perp_sell_pressure, signal.perp_cvd, thresholds
        ),
    }
    directions.discard(_Direction.NONE)
    return directions.pop() if len(directions) == 1 else _Direction.NONE


def _market_direction(
    buy_pressure: float | None,
    sell_pressure: float | None,
    cvd: float | None,
    thresholds: ClassificationThresholds,
) -> _Direction:
    buy, sell = buy_pressure or 0, sell_pressure or 0
    if buy >= thresholds.pressure and buy > sell and _cvd_confirms(cvd, _Direction.BUY, thresholds):
        return _Direction.BUY
    if (
        sell >= thresholds.pressure
        and sell > buy
        and _cvd_confirms(cvd, _Direction.SELL, thresholds)
    ):
        return _Direction.SELL
    return _Direction.NONE


def _cvd_confirms(
    cvd: float | None, direction: _Direction, thresholds: ClassificationThresholds
) -> bool:
    if cvd is None:
        return thresholds.allow_missing_cvd
    if direction is _Direction.BUY:
        return cvd > thresholds.cvd
    return cvd < -thresholds.cvd


def liquidity_fragility_score(
    *,
    depth_2: list[float],
    impact_10k: list[float],
    impact_50k: list[float],
    spread: list[float],
    capital_to_move_2: list[float],
    index: int,
) -> float:
    """Percentile-normalize each current-market measure; higher means more fragile."""
    components = (
        _percentile(depth_2, index, descending=True),
        _percentile(impact_10k, index),
        _percentile(impact_50k, index),
        _percentile(spread, index),
        _percentile(capital_to_move_2, index, descending=True),
    )
    return round(fmean(components) * 100, 2)


def liquidity_fragility_scores(
    metrics_by_symbol: Mapping[str, list[LiquidityMetrics]],
) -> dict[str, float]:
    """Normalize each active symbol's venue-aggregated raw liquidity against its peers."""
    raw_metrics = {
        symbol: _aggregate_liquidity(metrics) for symbol, metrics in metrics_by_symbol.items()
    }
    usable = {symbol: metric for symbol, metric in raw_metrics.items() if metric is not None}
    if not usable:
        return {symbol: 0.0 for symbol in metrics_by_symbol}
    symbols = sorted(usable)
    depth_2 = [usable[symbol][0] for symbol in symbols]
    impact_10k = [usable[symbol][1] for symbol in symbols]
    impact_50k = [usable[symbol][2] for symbol in symbols]
    spread = [usable[symbol][3] for symbol in symbols]
    capital_to_move_2 = [usable[symbol][4] for symbol in symbols]
    return {
        symbol: liquidity_fragility_score(
            depth_2=depth_2,
            impact_10k=impact_10k,
            impact_50k=impact_50k,
            spread=spread,
            capital_to_move_2=capital_to_move_2,
            index=index,
        )
        for index, symbol in enumerate(symbols)
    } | {symbol: 0.0 for symbol in metrics_by_symbol if symbol not in usable}


def _aggregate_liquidity(
    metrics: list[LiquidityMetrics],
) -> tuple[float, float, float, float, float] | None:
    usable = [
        metric
        for metric in metrics
        if metric.buy_impacts[10_000] is not None
        and metric.buy_impacts[50_000] is not None
        and metric.capital_to_move_up[2] is not None
    ]
    if not usable:
        return None
    return (
        fmean(metric.bid_depth_2 + metric.ask_depth_2 for metric in usable),
        fmean(float(metric.buy_impacts[10_000]) for metric in usable),
        fmean(float(metric.buy_impacts[50_000]) for metric in usable),
        fmean(metric.spread_percent for metric in usable),
        fmean(float(metric.capital_to_move_up[2]) for metric in usable),
    )


def activity_score(
    pressure_1m: float | None = None,
    pressure_5m: float | None = None,
    cvd_ratio: float | None = None,
    oi_change: float | None = None,
    funding: float | None = None,
    confirmed: bool = False,
    *,
    spot_pressure_1m: float | None = None,
    spot_pressure_5m: float | None = None,
    spot_cvd_ratio: float | None = None,
    perp_pressure_1m: float | None = None,
    perp_pressure_5m: float | None = None,
    perp_cvd_ratio: float | None = None,
) -> float:
    """Score market abnormality from bounded, direction-neutral available inputs.

    Positional inputs retain the original single-market call shape. New callers pass
    spot and perpetual inputs explicitly; absent markets are excluded from each
    component's average instead of being interpreted as zero activity.
    """
    if spot_pressure_1m is None:
        spot_pressure_1m = pressure_1m
    if spot_pressure_5m is None:
        spot_pressure_5m = pressure_5m
    if spot_cvd_ratio is None:
        spot_cvd_ratio = cvd_ratio

    pressure_component = _available_mean(
        (
            _market_pressure(spot_pressure_1m, spot_pressure_5m),
            _market_pressure(perp_pressure_1m, perp_pressure_5m),
        )
    )
    flow_component = _available_mean(
        (
            _optional_bounded_magnitude(spot_cvd_ratio),
            _optional_bounded_magnitude(perp_cvd_ratio),
        )
    )
    components = (
        50 * pressure_component,
        30 * flow_component,
        10 * _bounded_magnitude(oi_change),
        5 * _bounded_magnitude(funding, scale=1_000),
        5 if confirmed else 0,
    )
    return round(min(sum(components), 100), 2)


def _market_pressure(pressure_1m: float | None, pressure_5m: float | None) -> float | None:
    """Blend available pressure windows while keeping the short window responsive."""
    weighted = (
        (pressure_1m, 0.45),
        (pressure_5m, 0.55),
    )
    available = [(value, weight) for value, weight in weighted if value is not None]
    if not available:
        return None
    return sum(min(abs(value) / 3, 1.0) * weight for value, weight in available) / sum(
        weight for _, weight in available
    )


def _available_mean(values: Iterable[float | None]) -> float:
    available = [value for value in values if value is not None]
    return fmean(available) if available else 0.0


def _bounded_magnitude(value: float | None, *, scale: float = 1.0) -> float:
    return min(abs(value) * scale, 1.0) if value is not None else 0.0


def _optional_bounded_magnitude(value: float | None) -> float | None:
    return _bounded_magnitude(value) if value is not None else None


def _above(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _percentile(values: list[float], index: int, descending: bool = False) -> float:
    if not values or index < 0 or index >= len(values):
        raise ValueError("percentile values and index are required")
    ordered = sorted(values, reverse=descending)
    if len(ordered) == 1:
        return 0.5
    return ordered.index(values[index]) / (len(ordered) - 1)
