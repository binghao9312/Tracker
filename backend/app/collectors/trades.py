"""Reconnectable normalized aggressive-trade stream managers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable

import aiohttp

from app.models import MarketInstrument, MarketType, NormalizedTrade

logger = logging.getLogger(__name__)
TradeCallback = Callable[[NormalizedTrade], Awaitable[None]]


class _TradeManager:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        market: MarketType,
        instruments: Iterable[MarketInstrument],
        on_trade: TradeCallback,
    ) -> None:
        self._session = session
        self._market = market
        listed = tuple(instruments)
        self._instruments = {instrument.exchange_symbol: instrument for instrument in listed}
        if not self._instruments:
            raise ValueError("trade manager requires at least one instrument")
        if len(self._instruments) != len(listed):
            raise ValueError("trade manager has duplicate exchange symbols")
        if any(instrument.market is not market for instrument in self._instruments.values()):
            raise ValueError("trade manager instruments must share a market")
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
                    "trade_manager_reconnect: %s (%s): %s",
                    type(self).__name__,
                    self._market.value,
                    error,
                )
                await self._sleep_or_stop(stop, delay)
                delay = min(delay * 2, 30.0)
            except Exception:
                logger.exception(
                    "trade_manager_unexpected: %s (%s)",
                    type(self).__name__,
                    self._market.value,
                )
                await self._sleep_or_stop(stop, delay)
                delay = min(delay * 2, 30.0)

    @staticmethod
    async def _sleep_or_stop(stop: asyncio.Event, delay: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _stream(self, stop: asyncio.Event) -> None:
        raise NotImplementedError

    async def _publish_binance_trade(
        self, instrument: MarketInstrument, data: dict[str, object]
    ) -> None:
        price = _number(data, "p")
        quantity = _number(data, "q") * instrument.base_quantity_multiplier
        await self._on_trade(
            NormalizedTrade(
                exchange=instrument.exchange,
                symbol=instrument.symbol,
                market=instrument.market,
                timestamp=_integer(data, "T"),
                price=price,
                quantity=quantity,
                quote_value=price * quantity,
                side="SELL" if data["m"] else "BUY",
            )
        )

    async def _publish_okx_trade(
        self, instrument: MarketInstrument, record: dict[str, object]
    ) -> None:
        side = record.get("side")
        if side not in {"buy", "sell"}:
            raise ValueError("OKX trade message is invalid")
        price = _number(record, "px")
        quantity = _number(record, "sz") * instrument.base_quantity_multiplier
        await self._on_trade(
            NormalizedTrade(
                exchange=instrument.exchange,
                symbol=instrument.symbol,
                market=instrument.market,
                timestamp=_integer(record, "ts"),
                price=price,
                quantity=quantity,
                quote_value=price * quantity,
                side=side.upper(),
            )
        )


class BinanceTradeManager(_TradeManager):
    """Route a market's Binance aggregate trades over one combined stream."""

    async def _stream(self, stop: asyncio.Event) -> None:
        host = (
            "stream.binance.com:9443" if self._market is MarketType.SPOT else "fstream.binance.com"
        )
        path = "/stream" if self._market is MarketType.SPOT else "/market/stream"
        streams = "/".join(
            f"{instrument.exchange_symbol.lower()}@aggTrade"
            for instrument in self._instruments.values()
        )
        async with self._session.ws_connect(
            f"wss://{host}{path}?streams={streams}", heartbeat=20
        ) as websocket:
            async for message in websocket:
                if stop.is_set():
                    return
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise websocket.exception() or ConnectionError(
                        "Binance trades WebSocket closed"
                    )
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                envelope = _object(message.data)
                data = envelope.get("data")
                if not isinstance(data, dict) or data.get("e") != "aggTrade":
                    continue
                exchange_symbol = data.get("s")
                if not isinstance(exchange_symbol, str):
                    raise ValueError("Binance trade has no symbol")
                instrument = self._instruments.get(exchange_symbol)
                if instrument is not None:
                    await self._publish_binance_trade(instrument, data)
        if not stop.is_set():
            raise ConnectionError("Binance trades WebSocket disconnected")


class OkxTradeManager(_TradeManager):
    """Route a market's OKX trades over one multi-instrument subscription."""

    async def _stream(self, stop: asyncio.Event) -> None:
        async with self._session.ws_connect(
            "wss://ws.okx.com:8443/ws/v5/public", heartbeat=20
        ) as websocket:
            await websocket.send_json(
                {
                    "op": "subscribe",
                    "args": [
                        {"channel": "trades", "instId": instrument.exchange_symbol}
                        for instrument in self._instruments.values()
                    ],
                }
            )
            async for message in websocket:
                if stop.is_set():
                    return
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise websocket.exception() or ConnectionError("OKX trades WebSocket closed")
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                event = _object(message.data)
                if "event" in event:
                    continue
                argument = event.get("arg")
                records = event.get("data")
                if not isinstance(argument, dict) or argument.get("channel") != "trades":
                    continue
                exchange_symbol = argument.get("instId")
                if not isinstance(exchange_symbol, str):
                    raise ValueError("OKX trade has no instrument")
                instrument = self._instruments.get(exchange_symbol)
                if instrument is None:
                    continue
                if not isinstance(records, list):
                    raise ValueError("OKX trade message has invalid data")
                for record in records:
                    if not isinstance(record, dict):
                        raise ValueError("OKX trade message is invalid")
                    await self._publish_okx_trade(instrument, record)
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


def _number(data: dict[str, object], name: str) -> float:
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _integer(data: dict[str, object], name: str) -> int:
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be an integer")
    return int(value)
