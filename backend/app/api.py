"""Backend-owned HTTP and WebSocket API for the scanner UI."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from app.config import universe_path
from app.models import UniverseAsset
from app.paper_trading import PaperTradingEngine
from app.repository import MetricRepository, PaperTradeRepository
from app.universe import JsonMarketUniverseProvider


class ScannerRow(BaseModel):
    symbol: str
    market_cap_rank: int
    price: float | None = None
    activity_score: float | None = None
    liquidity_fragility: float | None = None


class DashboardDetail(BaseModel):
    """Canonical normalized state emitted by the live runtime."""

    model_config = ConfigDict(extra="forbid")

    price: float | None = None
    activity_score: float | None = None
    liquidity_fragility: float | None = None
    move_type: str | None = None
    cross_exchange_state: str | None = None
    oi_change_5m: float | None = None
    funding: float | None = None
    buy_pressure_1m: float | None = None
    buy_pressure_5m: float | None = None
    sell_pressure_1m: float | None = None
    sell_pressure_5m: float | None = None
    spot: dict[str, Any] = Field(default_factory=dict)
    perp: dict[str, Any] = Field(default_factory=dict)
    orderbooks: dict[str, Any] = Field(default_factory=dict)


class DashboardState:
    """Concurrency-safe latest-value cache; PostgreSQL remains the history source."""

    def __init__(
        self, universe: list[UniverseAsset], paper_engine: PaperTradingEngine | None = None
    ) -> None:
        self.universe = universe
        self.paper_engine = paper_engine
        self._rank_by_symbol = {f"{asset.symbol}USDT": asset.rank for asset in universe}
        self._rows: dict[str, ScannerRow] = {}
        self._details: dict[str, dict[str, Any]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def update_symbol(self, symbol: str, detail: dict[str, Any]) -> None:
        symbol = symbol.upper()
        canonical = DashboardDetail.model_validate(detail).model_dump()
        if symbol not in self._rank_by_symbol:
            raise ValueError(f"{symbol} is not in the monitored universe")
        async with self._lock:
            self._details[symbol] = canonical
            row = ScannerRow(
                symbol=symbol,
                market_cap_rank=self._rank_by_symbol[symbol],
                price=canonical["price"],
                activity_score=canonical["activity_score"],
                liquidity_fragility=canonical["liquidity_fragility"],
            )
            self._rows[symbol] = row
        paper_events = await self.paper_engine.process_update(symbol, canonical) if self.paper_engine else []
        await self._broadcast("scanner", {"type": "scanner", "data": row.model_dump()})
        await self._broadcast(f"symbol:{symbol}", {"type": "symbol", "data": canonical})
        for event in paper_events:
            await self._broadcast("paper", event)

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
    state: DashboardState | None = None,
    history_repository: MetricRepository | None = None,
    paper_repository: PaperTradeRepository | None = None,
    paper_engine: PaperTradingEngine | None = None,
) -> FastAPI:
    if state is None:
        state = DashboardState(JsonMarketUniverseProvider(universe_path()).load(), paper_engine)
    elif paper_engine is not None:
        state.paper_engine = paper_engine
    app = FastAPI(title="CEX Liquidity & Flow Tracker")
    app.state.dashboard = state
    app.state.history_repository = history_repository
    app.state.paper_repository = paper_repository

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

    def require_paper_repository() -> PaperTradeRepository:
        if paper_repository is None:
            raise HTTPException(status_code=503, detail="paper trade store is unavailable")
        return paper_repository

    @app.get("/api/paper/positions")
    async def get_paper_positions() -> list[dict[str, Any]]:
        return await require_paper_repository().get_open_positions()

    @app.get("/api/paper/trades")
    async def get_paper_trades(
        symbol: str | None = None,
        side: str | None = None,
        result: str | None = Query(default=None, pattern="^(open|win|loss)$"),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        return await require_paper_repository().list_trades(symbol, side, result, limit)

    @app.get("/api/paper/trades/{trade_id}")
    async def get_paper_trade(trade_id: int) -> dict[str, Any]:
        trade = await require_paper_repository().get_trade(trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="paper trade does not exist")
        history: dict[str, list[dict[str, Any]]] = {"market": [], "flow": [], "derivative": []}
        end = _timestamp(trade.get("closed_at")) or datetime.now().astimezone()
        if history_repository is not None:
            history = await history_repository.history_range(
                trade["symbol"],
                _timestamp(trade["opened_at"]),
                end,
                exchange=trade["exchange"],
                market=trade["market"],
            )
        return {
            "trade": trade,
            "entry_snapshot": trade["signal_snapshot"],
            "exit_snapshot": trade["exit_snapshot"],
            "history": history,
        }

    @app.get("/api/paper/stats")
    async def get_paper_stats() -> dict[str, Any]:
        return await require_paper_repository().stats()

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

    @app.websocket("/ws/paper")
    async def paper_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            positions = await require_paper_repository().get_open_positions()
            await websocket.send_json({"type": "OPEN", "data": positions})
            async for message in state.subscribe("paper"):
                await websocket.send_json(message)
        except WebSocketDisconnect:
            return

    return app



def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

app = create_app()
