"""Backend-owned HTTP and WebSocket API for the scanner UI."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.config import universe_path

from app.models import UniverseAsset
from app.repository import MetricRepository

from app.universe import JsonMarketUniverseProvider


class ScannerRow(BaseModel):
    symbol: str
    market_cap_rank: int
    price: float | None = None
    activity_score: float | None = None
    liquidity_fragility: float | None = None


class DashboardState:
    """Concurrency-safe latest-value cache; PostgreSQL remains the history source."""

    def __init__(self, universe: list[UniverseAsset]) -> None:
        self.universe = universe
        self._rows: dict[str, ScannerRow] = {}
        self._details: dict[str, dict[str, Any]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def update_symbol(self, symbol: str, detail: dict[str, Any]) -> None:
        async with self._lock:
            self._details[symbol] = detail
            row = ScannerRow(
                symbol=symbol,
                market_cap_rank=next(asset.rank for asset in self.universe if f"{asset.symbol}USDT" == symbol),
                price=detail.get("price"),
                activity_score=detail.get("activity_score"),
                liquidity_fragility=detail.get("liquidity_fragility"),
            )
            self._rows[symbol] = row
        await self._broadcast("scanner", {"type": "scanner", "data": row.model_dump()})
        await self._broadcast(f"symbol:{symbol}", {"type": "symbol", "data": detail})

    def scanner(self) -> list[ScannerRow]:
        return sorted(
            self._rows.values(),
            key=lambda row: (row.activity_score is None, -(row.activity_score or 0), row.symbol),
        )

    def detail(self, symbol: str) -> dict[str, Any] | None:
        return self._details.get(symbol)

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        self._subscribers[channel].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[channel].discard(queue)

    async def _broadcast(self, channel: str, message: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers[channel]):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)


def create_app(
    state: DashboardState | None = None, history_repository: MetricRepository | None = None
) -> FastAPI:
    if state is None:
        state = DashboardState(JsonMarketUniverseProvider(universe_path()).load())
    app = FastAPI(title="CEX Liquidity & Flow Tracker")
    app.state.dashboard = state
    app.state.history_repository = history_repository

    @app.get("/api/universe", response_model=list[UniverseAsset])
    async def get_universe() -> list[UniverseAsset]:
        return state.universe

    @app.get("/api/scanner", response_model=list[ScannerRow])
    async def get_scanner() -> list[ScannerRow]:
        return state.scanner()

    @app.get("/api/symbol/{symbol}")
    async def get_symbol(symbol: str) -> dict[str, Any]:
        detail = state.detail(symbol.upper())
        if detail is None:
            raise HTTPException(status_code=404, detail="symbol is not being monitored")
        return detail

    @app.get("/api/symbol/{symbol}/history")
    async def get_symbol_history(symbol: str) -> dict[str, list[dict[str, Any]]]:
        normalized_symbol = symbol.upper()
        if state.detail(normalized_symbol) is None:
            raise HTTPException(status_code=404, detail="symbol is not being monitored")
        if history_repository is None:
            raise HTTPException(status_code=503, detail="historical metric store is unavailable")
        return await history_repository.history(normalized_symbol)

    @app.websocket("/ws/scanner")
    async def scanner_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            await websocket.send_json({"type": "scanner", "data": [row.model_dump() for row in state.scanner()]})
            async for message in state.subscribe("scanner"):
                await websocket.send_json(message)
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/symbol/{symbol}")
    async def symbol_socket(websocket: WebSocket, symbol: str) -> None:
        await websocket.accept()
        channel = f"symbol:{symbol.upper()}"
        try:
            if detail := state.detail(symbol.upper()):
                await websocket.send_json({"type": "symbol", "data": detail})
            async for message in state.subscribe(channel):
                await websocket.send_json(message)
        except WebSocketDisconnect:
            return

    return app


app = create_app()
