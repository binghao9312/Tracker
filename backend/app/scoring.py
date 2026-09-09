"""Transparent cross-market classification and separate fragility/activity scores."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from statistics import fmean

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
    pressure: float
    cvd: float
    oi_change: float


def classify_move(signal: MarketSignal, thresholds: ClassificationThresholds) -> MoveType:
    spot_direction = _market_direction(
        signal.spot_buy_pressure, signal.spot_sell_pressure, signal.spot_cvd, thresholds
    )
    perp_direction = _market_direction(
        signal.perp_buy_pressure, signal.perp_sell_pressure, signal.perp_cvd, thresholds
    )
    perp_active = perp_direction is not _Direction.NONE and _above(signal.oi_change, thresholds.oi_change)
    if spot_direction is not _Direction.NONE and perp_active:
        return MoveType.MIXED if spot_direction is perp_direction else MoveType.NEUTRAL
    if spot_direction is not _Direction.NONE:
        return MoveType.SPOT_DRIVEN
    if perp_active:
        return MoveType.LEVERAGE_DRIVEN
    return MoveType.NEUTRAL


def cross_exchange_state(signals: list[MarketSignal], thresholds: ClassificationThresholds) -> CrossExchangeState:
    if len(signals) < 2:
        return CrossExchangeState.SINGLE_EXCHANGE
    directions = [_exchange_direction(signal, thresholds) for signal in signals]
    if directions[0] is not _Direction.NONE and all(
        direction is directions[0] for direction in directions[1:]
    ):
        return CrossExchangeState.CONFIRMED
    return CrossExchangeState.DIVERGENT

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
    if buy >= thresholds.pressure and buy > sell and (cvd is None or cvd >= thresholds.cvd):
        return _Direction.BUY
    if sell >= thresholds.pressure and sell > buy and (cvd is None or cvd <= -thresholds.cvd):
        return _Direction.SELL
    return _Direction.NONE



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
        symbol: _aggregate_liquidity(metrics)
        for symbol, metrics in metrics_by_symbol.items()
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


def _aggregate_liquidity(metrics: list[LiquidityMetrics]) -> tuple[float, float, float, float, float] | None:
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
    pressure_1m: float, pressure_5m: float, cvd_ratio: float, oi_change: float, funding: float, confirmed: bool
) -> float:
    """Visible bounded blend; pressure and normalized CVD magnitude are direction-neutral."""
    raw = 18 * min(pressure_1m, 3) + 22 * min(pressure_5m, 3) + 20 * min(abs(cvd_ratio), 1)
    raw += 20 * min(abs(oi_change), 1) + 10 * min(abs(funding) * 1_000, 1) + (10 if confirmed else 0)
    return round(min(raw, 100), 2)


def _above(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _percentile(values: list[float], index: int, descending: bool = False) -> float:
    if not values or index < 0 or index >= len(values):
        raise ValueError("percentile values and index are required")
    ordered = sorted(values, reverse=descending)
    if len(ordered) == 1:
        return 0.5
    return ordered.index(values[index]) / (len(ordered) - 1)
