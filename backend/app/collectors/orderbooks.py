"""Reconnectable WebSocket collectors that maintain exchange order books in RAM."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from time import time_ns

import aiohttp

from app.exchanges.binance import BinanceAdapter
from app.exchanges.okx import OkxAdapter
from app.models import MarketInstrument, OrderBook
from app.orderbook import LocalOrderBook, OrderBookSequenceGap, SequencedOrderBookSnapshot

logger = logging.getLogger(__name__)
OrderBookCallback = Callable[[OrderBook], Awaitable[None]]


class _ResyncingCollector:
    def __init__(self, book: LocalOrderBook, on_update: OrderBookCallback) -> None:
        self._book = book
        self._on_update = on_update

    async def run(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                await self._synchronize_and_stream(stop)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, ValueError, OrderBookSequenceGap) as error:
                logger.warning(
                    "orderbook_collector_resync",
                    extra={"collector": type(self).__name__, "error": str(error)},
                )
                await self._sleep_or_stop(stop, delay)
                delay = min(delay * 2, 30.0)

    async def _synchronize_and_stream(self, stop: asyncio.Event) -> None:
        raise NotImplementedError

    @staticmethod
    async def _sleep_or_stop(stop: asyncio.Event, delay: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _publish(self) -> None:
        await self._on_update(self._book.to_model(time_ns() // 1_000_000))


class BinanceOrderBookCollector(_ResyncingCollector):
    """Bootstrap from REST after opening WS, then enforce Binance ``pu`` continuity."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        adapter: BinanceAdapter,
        instrument: MarketInstrument,
        book: LocalOrderBook,
        on_update: OrderBookCallback,
    ) -> None:
        super().__init__(book, on_update)
        self._session = session
        self._adapter = adapter
        self._instrument = instrument

    async def _synchronize_and_stream(self, stop: asyncio.Event) -> None:
        host = (
            "stream.binance.com:9443"
            if self._instrument.market.value == "spot"
            else "fstream.binance.com"
        )
        stream = f"{self._instrument.exchange_symbol.lower()}@depth@100ms"
        async with self._session.ws_connect(
            f"wss://{host}/ws/{stream}", heartbeat=20
        ) as websocket:
            # The open socket buffers updates while REST establishes the baseline.
            self._book.bootstrap(await self._adapter.fetch_order_book_snapshot(self._instrument))
            is_first_increment = True
            async for message in websocket:
                if stop.is_set():
                    return
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise websocket.exception() or ConnectionError("Binance WebSocket closed")
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                event = _json_object(message.data)
                if event.get("e") != "depthUpdate":
                    continue
                try:
                    self._book.apply_binance_update(
                        first_sequence=_integer(event, "U"),
                        final_sequence=_integer(event, "u"),
                        previous_final_sequence=None if is_first_increment else _integer(event, "pu"),
                        bids=_depth_levels(event.get("b")),
                        asks=_depth_levels(event.get("a")),
                    )
                except (KeyError, ValueError, InvalidOperation) as error:
                    raise ValueError("invalid Binance depth update") from error
                is_first_increment = False
                await self._publish()
            if not stop.is_set():
                raise ConnectionError("Binance WebSocket disconnected")


class OkxOrderBookCollector(_ResyncingCollector):
    """Bootstrap from REST and enforce ``prevSeqId`` continuity on OKX updates."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        adapter: OkxAdapter,
        instrument: MarketInstrument,
        book: LocalOrderBook,
        on_update: OrderBookCallback,
    ) -> None:
        super().__init__(book, on_update)
        self._session = session
        self._adapter = adapter
        self._instrument = instrument

    async def _synchronize_and_stream(self, stop: asyncio.Event) -> None:
        async with self._session.ws_connect(
            "wss://ws.okx.com:8443/ws/v5/public", heartbeat=20
        ) as websocket:
            await websocket.send_json(
                {
                    "op": "subscribe",
                    "args": [{"channel": "books", "instId": self._instrument.exchange_symbol}],
                }
            )
            self._book.bootstrap(await self._adapter.fetch_order_book_snapshot(self._instrument))
            async for message in websocket:
                if stop.is_set():
                    return
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise websocket.exception() or ConnectionError("OKX WebSocket closed")
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                event = _json_object(message.data)
                argument = event.get("arg")
                if not isinstance(argument, dict) or argument.get("channel") != "books":
                    continue
                for update in event.get("data", []):
                    if not isinstance(update, dict):
                        raise ValueError("invalid OKX depth update")
                    if event.get("action") == "snapshot":
                        self._book.bootstrap(
                            SequencedOrderBookSnapshot(
                                exchange=self._instrument.exchange,
                                symbol=self._instrument.symbol,
                                market=self._instrument.market,
                                sequence=_integer(update, "seqId"),
                                timestamp=_integer(update, "ts"),
                                bids=_depth_levels(update.get("bids")),
                                asks=_depth_levels(update.get("asks")),
                            )
                        )
                    else:
                        self._book.apply_okx_update(
                            sequence=_integer(update, "seqId"),
                            previous_sequence=_integer(update, "prevSeqId"),
                            bids=_depth_levels(update.get("bids")),
                            asks=_depth_levels(update.get("asks")),
                        )
                    await self._publish()
            if not stop.is_set():
                raise ConnectionError("OKX WebSocket disconnected")


def _json_object(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("WebSocket message is not JSON") from error
    if not isinstance(value, dict):
        raise ValueError("WebSocket message is not an object")
    return value


def _integer(data: dict[str, object], name: str) -> int:
    value = data.get(name)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _depth_levels(raw_levels: object) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(raw_levels, list):
        raise ValueError("depth update has no price levels")
    levels: list[tuple[Decimal, Decimal]] = []
    for level in raw_levels:
        if not isinstance(level, list) or len(level) < 2:
            raise ValueError("depth update level is invalid")
        levels.append((Decimal(str(level[0])), Decimal(str(level[1]))))
    return levels
