"""Reconnectable normalized aggressive-trade collectors."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from time import time_ns

import aiohttp

from app.models import MarketInstrument, NormalizedTrade

logger = logging.getLogger(__name__)
TradeCallback = Callable[[NormalizedTrade], Awaitable[None]]


class _TradeCollector:
    def __init__(self, session: aiohttp.ClientSession, on_trade: TradeCallback) -> None:
        self._session = session
        self._on_trade = on_trade

    async def run(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                await self._stream(stop)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, ValueError, KeyError) as error:
                logger.warning(
                    "trade_collector_reconnect",
                    extra={"collector": type(self).__name__, "error": str(error)},
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 30.0)

    async def _stream(self, stop: asyncio.Event) -> None:
        raise NotImplementedError


class BinanceTradeCollector(_TradeCollector):
    def __init__(
        self,
        session: aiohttp.ClientSession,
        instrument: MarketInstrument,
        on_trade: TradeCallback,
    ) -> None:
        super().__init__(session, on_trade)
        self._instrument = instrument

    async def _stream(self, stop: asyncio.Event) -> None:
        host = "stream.binance.com:9443" if self._instrument.market.value == "spot" else "fstream.binance.com"
        stream = f"{self._instrument.exchange_symbol.lower()}@aggTrade"
        async with self._session.ws_connect(f"wss://{host}/ws/{stream}", heartbeat=20) as websocket:
            async for message in websocket:
                if stop.is_set():
                    return
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise websocket.exception() or ConnectionError("Binance trades WebSocket closed")
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                data = _object(message.data)
                if data.get("e") != "aggTrade":
                    continue
                price = float(data["p"])
                quantity = float(data["q"]) * self._instrument.base_quantity_multiplier
                await self._on_trade(
                    NormalizedTrade(
                        exchange=self._instrument.exchange,
                        symbol=self._instrument.symbol,
                        market=self._instrument.market,
                        timestamp=int(data["T"]),
                        price=price,
                        quantity=quantity,
                        quote_value=price * quantity,
                        side="SELL" if data["m"] else "BUY",
                    )
                )
        if not stop.is_set():
            raise ConnectionError("Binance trades WebSocket disconnected")


class OkxTradeCollector(_TradeCollector):
    def __init__(
        self,
        session: aiohttp.ClientSession,
        instrument: MarketInstrument,
        on_trade: TradeCallback,
    ) -> None:
        super().__init__(session, on_trade)
        self._instrument = instrument

    async def _stream(self, stop: asyncio.Event) -> None:
        async with self._session.ws_connect("wss://ws.okx.com:8443/ws/v5/public", heartbeat=20) as websocket:
            await websocket.send_json(
                {"op": "subscribe", "args": [{"channel": "trades", "instId": self._instrument.exchange_symbol}]}
            )
            async for message in websocket:
                if stop.is_set():
                    return
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise websocket.exception() or ConnectionError("OKX trades WebSocket closed")
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                event = _object(message.data)
                argument = event.get("arg")
                records = event.get("data")
                if not isinstance(argument, dict) or argument.get("channel") != "trades":
                    continue
                if not isinstance(records, list):
                    raise ValueError("OKX trade message has invalid data")
                for record in records:
                    if not isinstance(record, dict) or record.get("side") not in {"buy", "sell"}:
                        raise ValueError("OKX trade message is invalid")
                    price = float(record["px"])
                    quantity = float(record["sz"]) * self._instrument.base_quantity_multiplier
                    await self._on_trade(
                        NormalizedTrade(
                            exchange=self._instrument.exchange,
                            symbol=self._instrument.symbol,
                            market=self._instrument.market,
                            timestamp=int(record["ts"]),
                            price=price,
                            quantity=quantity,
                            quote_value=price * quantity,
                            side=record["side"].upper(),
                        )
                    )
        if not stop.is_set():
            raise ConnectionError("OKX trades WebSocket disconnected")


def _object(raw: str) -> dict[str, object]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("trade message is not JSON") from error
    if not isinstance(data, dict):
        raise ValueError("trade message is not an object")
    return data
