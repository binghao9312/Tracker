"""Transparent cross-market classification and separate fragility/activity scores."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from statistics import fmean


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
    perp_buy_pressure: float | None
    spot_cvd: float
    perp_cvd: float
    oi_change: float | None


@dataclass(frozen=True)
class ClassificationThresholds:
    pressure: float
    cvd: float
    oi_change: float


def classify_move(signal: MarketSignal, thresholds: ClassificationThresholds) -> MoveType:
    spot_active = _above(signal.spot_buy_pressure, thresholds.pressure) and signal.spot_cvd >= thresholds.cvd
    perp_active = (
        _above(signal.perp_buy_pressure, thresholds.pressure)
        and signal.perp_cvd >= thresholds.cvd
        and _above(signal.oi_change, thresholds.oi_change)
    )
    if spot_active and perp_active:
        return MoveType.MIXED
    if spot_active:
        return MoveType.SPOT_DRIVEN
    if perp_active:
        return MoveType.LEVERAGE_DRIVEN
    return MoveType.NEUTRAL


def cross_exchange_state(signals: list[MarketSignal], thresholds: ClassificationThresholds) -> CrossExchangeState:
    if len(signals) < 2:
        return CrossExchangeState.SINGLE_EXCHANGE
    active = [
        _above(signal.spot_buy_pressure, thresholds.pressure)
        or _above(signal.perp_buy_pressure, thresholds.pressure)
        for signal in signals
    ]
    return CrossExchangeState.CONFIRMED if all(active) else CrossExchangeState.DIVERGENT


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


def activity_score(
    pressure_1m: float, pressure_5m: float, cvd_change: float, oi_change: float, funding: float, confirmed: bool
) -> float:
    """Visible bounded blend; intentionally independent from liquidity fragility."""
    raw = 18 * min(pressure_1m, 3) + 22 * min(pressure_5m, 3) + 20 * min(abs(cvd_change), 1)
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
