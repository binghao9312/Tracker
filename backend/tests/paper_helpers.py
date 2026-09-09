from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


class MemoryPaperRepository:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.next_id = 1

    async def open_trade(self, values: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(values) | {"id": self.next_id}
        self.rows[self.next_id] = row
        self.next_id += 1
        return deepcopy(row)

    async def close_trade(self, trade_id: int, values: Mapping[str, Any]) -> dict[str, Any]:
        self.rows[trade_id].update(values)
        return deepcopy(self.rows[trade_id])

    async def get_open_positions(self) -> list[dict[str, Any]]:
        return [deepcopy(row) for row in self.rows.values() if row["status"] == "OPEN"]

    async def get_recent_closed_positions(self, since: object) -> list[dict[str, Any]]:
        return [
            deepcopy(row)
            for row in self.rows.values()
            if row["status"] == "CLOSED"
            and row.get("closed_at") is not None
            and row["closed_at"] >= since
        ]

    async def append_event(self, **values: Any) -> dict[str, Any]:
        row = dict(values) | {"id": len(self.events) + 1}
        self.events.append(row)
        return deepcopy(row)


def detail(price: float = 100.0, score: float = 90.0) -> dict[str, Any]:
    return {
        "price": price,
        "activity_score": score,
        "liquidity_fragility": 40.0,
        "move_type": "MIXED",
        "cross_exchange_state": "CONFIRMED",
        "spot": {
            "buy_pressure_5m": 3.0,
            "sell_pressure_5m": 1.0,
            "buy_volume_5m": 800.0,
            "sell_volume_5m": 200.0,
            "cvd_5m": 600.0,
        },
        "perp": {
            "buy_pressure_5m": 2.5,
            "sell_pressure_5m": 1.0,
            "buy_volume_5m": 400.0,
            "sell_volume_5m": 100.0,
            "cvd_5m": 300.0,
        },
        "orderbooks": {"binance": {"perp": {"bids": [[99.0, 100.0]], "asks": [[101.0, 100.0]]}}},
    }
