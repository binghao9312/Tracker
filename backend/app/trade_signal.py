"""Direction inference for simulated perpetual paper trades.

Activity measures how unusual a market is; this module deliberately derives direction
only from normalized flow pressure and volume imbalance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping


class TradeBias(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


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


def calculate_trade_signal(detail: Mapping[str, Any]) -> TradeSignal:
    """Derive a bias from tolerant, exchange-normalized dashboard detail maps."""
    spot = _market_values(detail, "spot")
    perp = _market_values(detail, "perp")
    all_values = [detail, *spot, *perp]
    buy_pressure = max((_number(item, "buy_pressure_5m", "buy_pressure") for item in all_values), default=0.0)
    sell_pressure = max((_number(item, "sell_pressure_5m", "sell_pressure") for item in all_values), default=0.0)
    buy_volume = sum(_number(item, "buy_volume_5m") for item in [*spot, *perp])
    sell_volume = sum(_number(item, "sell_volume_5m") for item in [*spot, *perp])
    if buy_volume + sell_volume == 0:
        buy_volume = _number(detail, "buy_volume_5m")
        sell_volume = _number(detail, "sell_volume_5m")
    total = buy_volume + sell_volume
    delta_ratio = (buy_volume - sell_volume) / total if total else 0.0
    if buy_pressure >= 2.0 and buy_pressure >= sell_pressure * 1.5 and delta_ratio >= 0.15:
        bias = TradeBias.LONG
    elif sell_pressure >= 2.0 and sell_pressure >= buy_pressure * 1.5 and delta_ratio <= -0.15:
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
