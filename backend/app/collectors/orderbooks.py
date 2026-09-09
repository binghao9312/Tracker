"""Reconnectable WebSocket managers that maintain exchange order books in RAM."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal, InvalidOperation
from time import time_ns

import aiohttp

from app.exchanges.binance import BinanceAdapter
from app.exchanges.okx import OkxAdapter
from app.models import MarketInstrument, MarketType, OrderBook
from app.orderbook import LocalOrderBook, OrderBookSequenceGap, SequencedOrderBookSnapshot

logger = logging.getLogger(__name__)
OrderBookCallback = Callable[[OrderBook], Awaitable[None]]
BookEntry = tuple[MarketInstrument, LocalOrderBook]


class _ResyncingCollector:
    """Compatibility base for the legacy one-book collector test contract."""

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


class _OrderBookManager:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        market: MarketType,
        books: Iterable[BookEntry],
        on_update: OrderBookCallback,
    ) -> None:
        self._session = session
        self._market = market
        entries = tuple(books)
        self._books = {
            instrument.exchange_symbol: (instrument, book) for instrument, book in entries
        }
        if not self._books:
            raise ValueError("order book manager requires at least one instrument")
        if len(self._books) != len(entries):
            raise ValueError("order book manager has duplicate exchange symbols")
        if any(instrument.market is not market for instrument, _ in self._books.values()):
            raise ValueError("order book manager instruments must share a market")
        self._on_update = on_update
        self._last_published_ms: dict[str, int] = {}

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
                    "orderbook_manager_resync: %s (%s): %s",
                    type(self).__name__,
                    self._market.value,
                    error,
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

    async def _publish(self, book: LocalOrderBook, *, force: bool = False) -> None:
        now_ms = time_ns() // 1_000_000
        if not force and book.sequence is not None:
            last_ms = self._last_published_ms.get(book.symbol, 0)
            if now_ms - last_ms < 100:
                return
            self._last_published_ms[book.symbol] = now_ms
        await self._on_update(book.to_model(now_ms))

class BinanceOrderBookManager(_OrderBookManager):
    """Synchronize one Binance market group from a combined depth stream."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        adapter: BinanceAdapter,
        market: MarketType,
        books: Iterable[BookEntry],
        on_update: OrderBookCallback,
    ) -> None:
        super().__init__(session, market, books, on_update)
        self._adapter = adapter

    async def _bootstrap_all(self) -> None:
        entries = tuple(self._books.values())
        snapshots = await asyncio.gather(
            *(self._adapter.fetch_order_book_snapshot(instrument) for instrument, _ in entries)
        )
        for (_, book), snapshot in zip(entries, snapshots, strict=True):
            book.bootstrap(snapshot)

    async def _synchronize_and_stream(self, stop: asyncio.Event) -> None:
        host = (
            "stream.binance.com:9443" if self._market is MarketType.SPOT else "fstream.binance.com"
        )
        streams = "/".join(
            f"{instrument.exchange_symbol.lower()}@depth@100ms"
            for instrument, _ in self._books.values()
        )
        async with self._session.ws_connect(
            f"wss://{host}/stream?streams={streams}", heartbeat=20
        ) as websocket:
            # The opened socket buffers increments while every book gets a fresh REST baseline.
            await self._bootstrap_all()
            first_increment = {exchange_symbol: True for exchange_symbol in self._books}
            async for message in websocket:
                if stop.is_set():
                    return
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise websocket.exception() or ConnectionError("Binance WebSocket closed")
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                envelope = _json_object(message.data)
                event = envelope.get("data")
                if not isinstance(event, dict) or event.get("e") != "depthUpdate":
                    continue
                exchange_symbol = event.get("s")
                if not isinstance(exchange_symbol, str):
                    raise ValueError("Binance depth update has no symbol")
                entry = self._books.get(exchange_symbol)
                if entry is None:
                    continue
                instrument, book = entry
                try:
                    final_sequence = _integer(event, "u")
                    if book.sequence is not None and final_sequence <= book.sequence:
                        continue
                    if self._market is MarketType.SPOT:
                        book.apply_binance_spot_update(
                            first_sequence=_integer(event, "U"),
                            final_sequence=final_sequence,
                            bids=_depth_levels(event.get("b")),
                            asks=_depth_levels(event.get("a")),
                        )
                    else:
                        book.apply_binance_futures_update(
                            first_sequence=_integer(event, "U"),
                            final_sequence=final_sequence,
                            previous_final_sequence=(
                                None if first_increment[exchange_symbol] else _integer(event, "pu")
                            ),
                            bids=_depth_levels(event.get("b")),
                            asks=_depth_levels(event.get("a")),
                        )
                except (KeyError, ValueError, InvalidOperation) as error:
                    raise ValueError("invalid Binance depth update") from error
                is_initial = first_increment[exchange_symbol]
                first_increment[exchange_symbol] = False
                await self._publish(book, force=is_initial)
        if not stop.is_set():
            raise ConnectionError("Binance WebSocket disconnected")


class OkxOrderBookManager(_OrderBookManager):
    """Synchronize one OKX market group from a multi-instrument books stream."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        adapter: OkxAdapter,
        market: MarketType,
        books: Iterable[BookEntry],
        on_update: OrderBookCallback,
    ) -> None:
        super().__init__(session, market, books, on_update)
        self._adapter = adapter

    async def _bootstrap_all(self) -> None:
        entries = tuple(self._books.values())
        sem = asyncio.Semaphore(5)

        async def fetch(instrument: MarketInstrument, book: LocalOrderBook) -> None:
            async with sem:
                for attempt in range(4):
                    try:
                        snapshot = await self._adapter.fetch_order_book_snapshot(instrument)
                        book.bootstrap(snapshot)
                        return
                    except ValueError as error:
                        if "50011" in str(error) or "rate limit" in str(error).lower():
                            await asyncio.sleep(0.4 * (attempt + 1))
                            continue
                        raise

        await asyncio.gather(*(fetch(instrument, book) for instrument, book in entries))

    async def _synchronize_and_stream(self, stop: asyncio.Event) -> None:
        async with self._session.ws_connect(
            "wss://ws.okx.com:8443/ws/v5/public", heartbeat=20
        ) as websocket:
            await websocket.send_json(
                {
                    "op": "subscribe",
                    "args": [
                        {"channel": "books", "instId": instrument.exchange_symbol}
                        for instrument, _ in self._books.values()
                    ],
                }
            )
            # Subscription snapshots are ignored: REST is the baseline for this attempt.
            await self._bootstrap_all()
            async for message in websocket:
                if stop.is_set():
                    return
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise websocket.exception() or ConnectionError("OKX WebSocket closed")
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                event = _json_object(message.data)
                if "event" in event:
                    continue
                argument = event.get("arg")
                if not isinstance(argument, dict) or argument.get("channel") != "books":
                    continue
                exchange_symbol = argument.get("instId")
                if not isinstance(exchange_symbol, str):
                    raise ValueError("OKX depth update has no instrument")
                entry = self._books.get(exchange_symbol)
                if entry is None:
                    continue
                records = event.get("data")
                if not isinstance(records, list):
                    raise ValueError("invalid OKX depth update")
                is_snapshot = event.get("action") == "snapshot"
                instrument, book = entry
                if not is_snapshot and book.sequence is None:
                    continue
                for update in records:
                    if not isinstance(update, dict):
                        raise ValueError("invalid OKX depth update")
                    try:
                        sequence = _integer(update, "seqId")
                        previous_sequence = None if is_snapshot else _integer(update, "prevSeqId")
                        if (
                            not is_snapshot
                            and book.sequence is not None
                            and previous_sequence != book.sequence
                            and sequence <= book.sequence
                        ):
                            continue
                        bids = _depth_levels(
                            update.get("bids"),
                            quantity_multiplier=instrument.base_quantity_multiplier,
                        )
                        asks = _depth_levels(
                            update.get("asks"),
                            quantity_multiplier=instrument.base_quantity_multiplier,
                        )
                        if is_snapshot:
                            book.bootstrap(
                                SequencedOrderBookSnapshot(
                                    exchange=instrument.exchange,
                                    symbol=instrument.symbol,
                                    market=instrument.market,
                                    sequence=sequence,
                                    timestamp=_integer(update, "ts"),
                                    bids=bids,
                                    asks=asks,
                                )
                            )
                        else:
                            book.apply_okx_update(
                                sequence=sequence,
                                previous_sequence=previous_sequence,
                                bids=bids,
                                asks=asks,
                            )
                    except (KeyError, ValueError, InvalidOperation) as error:
                        raise ValueError("invalid OKX depth update") from error
                    await self._publish(book, force=is_snapshot)
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


def _depth_levels(
    raw_levels: object, *, quantity_multiplier: float = 1.0
) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(raw_levels, list):
        raise ValueError("depth update has no price levels")
    multiplier = Decimal(str(quantity_multiplier))
    levels: list[tuple[Decimal, Decimal]] = []
    for level in raw_levels:
        if not isinstance(level, list) or len(level) < 2:
            raise ValueError("depth update level is invalid")
        levels.append((Decimal(str(level[0])), Decimal(str(level[1])) * multiplier))
    return levels
