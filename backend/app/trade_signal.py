"""Direction inference for simulated perpetual paper trades.

Activity measures how unusual a market is; this module deliberately derives direction
only from normalized flow pressure and volume imbalance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any

import yaml


class TradeBias(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


@dataclass(frozen=True)
class TradeSignalThresholds:
    pressure: float = 2.0
    pressure_dominance_ratio: float = 1.5
    delta_ratio: float = 0.15

    def __post_init__(self) -> None:
        object.__setattr__(self, "pressure", _nonnegative_float("pressure", self.pressure))
        dominance = _finite_float("pressure_dominance_ratio", self.pressure_dominance_ratio)
        if dominance < 1:
            raise ValueError("pressure_dominance_ratio must be at least 1")
        object.__setattr__(self, "pressure_dominance_ratio", dominance)
        delta_ratio = _finite_float("delta_ratio", self.delta_ratio)
        if not 0 <= delta_ratio <= 1:
            raise ValueError("delta_ratio must be between 0 and 1")
        object.__setattr__(self, "delta_ratio", delta_ratio)


def load_trade_signal_thresholds(path: Path) -> TradeSignalThresholds:
    """Load validated paper-trading signal thresholds from the scoring configuration."""
    with path.open(encoding="utf-8") as file:
        configured = yaml.safe_load(file)
    if not isinstance(configured, Mapping):
        raise ValueError("scoring configuration must be a mapping")
    values = configured.get("trade_signal")
    if not isinstance(values, Mapping):
        raise ValueError("trade_signal configuration must be a mapping")
    required = {"pressure", "pressure_dominance_ratio", "delta_ratio"}
    missing = required - values.keys()
    if missing:
        raise ValueError(f"trade_signal configuration is missing: {', '.join(sorted(missing))}")
    try:
        return TradeSignalThresholds(**dict(values))
    except TypeError as exc:
        raise ValueError("trade_signal configuration has unsupported values") from exc


def _finite_float(field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _nonnegative_float(field: str, value: object) -> float:
    number = _finite_float(field, value)
    if number < 0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return number


_DEFAULT_THRESHOLDS = TradeSignalThresholds()


@dataclass(frozen=True)
class TradeSignal:
    bias: TradeBias
    buy_pressure: float
    sell_pressure: float
    delta_ratio: float
    buy_volume_5m: float
    sell_volume_5m: float
    oi_change_5m: float | None
    cross_exchange_state: str | None
    confidence: float

    def snapshot(self) -> dict[str, Any]:
        return asdict(self) | {"bias": self.bias.value}


def calculate_trade_signal(
    detail: Mapping[str, Any], thresholds: TradeSignalThresholds = _DEFAULT_THRESHOLDS
) -> TradeSignal:
    """Derive a bias from tolerant, exchange-normalized dashboard detail maps."""
    spot = _market_values(detail, "spot")
    perp = _market_values(detail, "perp")
    all_values = [detail, *spot, *perp]
    buy_pressure = max(
        (_number(item, "buy_pressure_5m", "buy_pressure") for item in all_values), default=0.0
    )
    sell_pressure = max(
        (_number(item, "sell_pressure_5m", "sell_pressure") for item in all_values), default=0.0
    )
    buy_volume = sum(_number(item, "buy_volume_5m") for item in [*spot, *perp])
    sell_volume = sum(_number(item, "sell_volume_5m") for item in [*spot, *perp])
    if buy_volume + sell_volume == 0:
        buy_volume = _number(detail, "buy_volume_5m")
        sell_volume = _number(detail, "sell_volume_5m")
    total = buy_volume + sell_volume
    delta_ratio = (buy_volume - sell_volume) / total if total else 0.0
    if (
        buy_pressure >= thresholds.pressure
        and buy_pressure >= sell_pressure * thresholds.pressure_dominance_ratio
        and delta_ratio >= thresholds.delta_ratio
    ):
        bias = TradeBias.LONG
    elif (
        sell_pressure >= thresholds.pressure
        and sell_pressure >= buy_pressure * thresholds.pressure_dominance_ratio
        and delta_ratio <= -thresholds.delta_ratio
    ):
        bias = TradeBias.SHORT
    else:
        bias = TradeBias.NONE
    oi_change = _optional_number(detail, "oi_change_5m")
    confidence = abs(delta_ratio) + max(buy_pressure, sell_pressure) / 10
    if bias is not TradeBias.NONE and oi_change is not None and oi_change > 0:
        confidence += 0.1
    return TradeSignal(
        bias=bias,
        buy_pressure=buy_pressure,
        sell_pressure=sell_pressure,
        delta_ratio=delta_ratio,
        buy_volume_5m=buy_volume,
        sell_volume_5m=sell_volume,
        oi_change_5m=oi_change,
        cross_exchange_state=_string(detail, "cross_exchange_state"),
        confidence=round(confidence, 4),
    )


def _market_values(detail: Mapping[str, Any], market: str) -> list[Mapping[str, Any]]:
    values: list[Mapping[str, Any]] = []
    direct = detail.get(market)
    if isinstance(direct, Mapping):
        values.append(direct)
    markets = detail.get("markets")
    if isinstance(markets, Mapping):
        nested = markets.get(market)
        if isinstance(nested, Mapping):
            values.append(nested)
        elif isinstance(nested, list):
            values.extend(item for item in nested if isinstance(item, Mapping))
    for exchange in ("binance", "okx"):
        exchange_data = detail.get(exchange)
        if isinstance(exchange_data, Mapping):
            nested = exchange_data.get(market)
            if isinstance(nested, Mapping):
                values.append(nested)
    return values


def _number(values: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _optional_number(values: Mapping[str, Any], key: str) -> float | None:
    value = values.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _string(values: Mapping[str, Any], key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) else None
