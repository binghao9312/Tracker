"""Reconnectable WebSocket managers that maintain exchange order books in RAM."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal, InvalidOperation
from time import monotonic, time_ns

import aiohttp

from app.exchanges.binance import BinanceAdapter
from app.exchanges.okx import OkxAdapter
from app.models import MarketInstrument, MarketType, OrderBook
from app.orderbook import LocalOrderBook, OrderBookSequenceGap, SequencedOrderBookSnapshot

logger = logging.getLogger(__name__)
OrderBookCallback = Callable[[OrderBook], Awaitable[None]]
BookEntry = tuple[MarketInstrument, LocalOrderBook]

BINANCE_BOOK_CHUNK_SIZE = 20
BINANCE_SNAPSHOT_CONCURRENCY = 3

BINANCE_SNAPSHOT_MIN_INTERVAL_SECONDS = 0.25
SnapshotFetcher = Callable[[], Awaitable[SequencedOrderBookSnapshot]]


class BinanceSnapshotScheduler:
    """Bound and pace every Binance depth bootstrap across all book chunks."""

    def __init__(
        self,
        *,
        concurrency: int = BINANCE_SNAPSHOT_CONCURRENCY,
        min_interval_seconds: float = BINANCE_SNAPSHOT_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._min_interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._blocked_until = 0.0
        self._backoff = 2.0

    async def fetch(self, fetcher: SnapshotFetcher) -> SequencedOrderBookSnapshot:
        async with self._semaphore:
            await self._wait_for_turn()
            try:
                snapshot = await fetcher()
            except aiohttp.ClientResponseError as error:
                await self._report_error(error)
                raise
            else:
                await self._report_success()
                return snapshot

    async def _wait_for_turn(self) -> None:
        while True:
            async with self._lock:
                now = monotonic()
                ready_at = max(self._next_request_at, self._blocked_until)
                if ready_at <= now:
                    self._next_request_at = now + self._min_interval
                    return
                delay = ready_at - now
            await asyncio.sleep(delay)

    async def _report_success(self) -> None:
        async with self._lock:
            if monotonic() >= self._blocked_until:
                self._backoff = 2.0

    async def _report_error(self, error: aiohttp.ClientResponseError) -> None:
        if error.status not in (418, 429):
            return
        retry_after = _retry_after_seconds(error)
        async with self._lock:
            delay = (
                max(retry_after, 60.0)
                if error.status == 418
                else max(retry_after, self._backoff)
            )
            self._blocked_until = max(self._blocked_until, monotonic() + delay)
            if error.status == 429:
                self._backoff = min(self._backoff * 2, 60.0)
            logger.warning("binance_snapshot_%s: cooldown=%.1fs", error.status, delay)


def _retry_after_seconds(error: aiohttp.ClientResponseError) -> float:
    raw = error.headers.get("Retry-After") if error.headers else None
    try:
        return max(0.0, float(raw)) if raw is not None else 0.0
    except ValueError:
        return 0.0


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
                await self._mark_unavailable()
                task = asyncio.current_task()
                logger.warning(
                    "orderbook_chunk_resync: %s: %s",
                    task.get_name() if task is not None else self._market.value,
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

    async def _publish(
        self, book: LocalOrderBook, *, source_timestamp: int, force: bool = False
    ) -> None:
        received_at = time_ns() // 1_000_000
        if not force and book.sequence is not None:
            last_ms = self._last_published_ms.get(book.symbol, 0)
            if received_at - last_ms < 100:
                return
            self._last_published_ms[book.symbol] = received_at
        await self._on_update(book.to_model(source_timestamp, received_at=received_at))

    async def _mark_unavailable(self) -> None:
        timestamp = time_ns() // 1_000_000
        for instrument, _ in self._books.values():
            await self._on_update(
                OrderBook(
                    exchange=instrument.exchange,
                    symbol=instrument.symbol,
                    market=instrument.market,
                    timestamp=timestamp,
                    received_at=timestamp,
                    available=False,
                    bids=[],
                    asks=[],
                )
            )

class BinanceOrderBookManager(_OrderBookManager):
    """Synchronize one Binance market group from a combined depth stream."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        adapter: BinanceAdapter,
        market: MarketType,
        books: Iterable[BookEntry],
        on_update: OrderBookCallback,
        snapshot_scheduler: BinanceSnapshotScheduler | None = None,
    ) -> None:
        super().__init__(session, market, books, on_update)
        self._adapter = adapter
        self._snapshot_scheduler = snapshot_scheduler or BinanceSnapshotScheduler()

    async def _bootstrap_all(self) -> None:
        entries = tuple(self._books.values())

        async def fetch(instrument: MarketInstrument, book: LocalOrderBook) -> None:
            snapshot = await self._snapshot_scheduler.fetch(
                lambda: self._adapter.fetch_order_book_snapshot(instrument)
            )
            book.bootstrap(snapshot)

        tasks = [
            asyncio.create_task(fetch(instrument, book)) for instrument, book in entries
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    async def _synchronize_and_stream(self, stop: asyncio.Event) -> None:
        host = (
            "stream.binance.com:9443" if self._market is MarketType.SPOT else "fstream.binance.com"
        )
        path = "/stream" if self._market is MarketType.SPOT else "/public/stream"
        streams = "/".join(
            f"{instrument.exchange_symbol.lower()}@depth@100ms"
            for instrument, _ in self._books.values()
        )
        async with self._session.ws_connect(
            f"wss://{host}{path}?streams={streams}", heartbeat=20
        ) as websocket:
            task = asyncio.current_task()
            logger.info(
                "orderbook_chunk_connected: %s",
                task.get_name() if task is not None else self._market.value,
            )
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
                _instrument, book = entry
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
                    source_timestamp = (
                        _integer(event, "E")
                        if "E" in event
                        else _integer(event, "T")
                        if "T" in event
                        else time_ns() // 1_000_000
                    )
                except (KeyError, ValueError, InvalidOperation) as error:
                    raise ValueError("invalid Binance depth update") from error
                is_initial = first_increment[exchange_symbol]
                first_increment[exchange_symbol] = False
                await self._publish(
                    book, source_timestamp=source_timestamp, force=is_initial
                )
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
            task = asyncio.current_task()
            logger.info(
                "orderbook_chunk_connected: %s",
                task.get_name() if task is not None else self._market.value,
            )
            await websocket.send_json(
                {
                    "op": "subscribe",
                    "args": [
                        {"channel": "books", "instId": instrument.exchange_symbol}
                        for instrument, _ in self._books.values()
                    ],
                }
            )
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
                        source_timestamp = (
                            _integer(update, "ts")
                            if "ts" in update
                            else time_ns() // 1_000_000
                        )
                        if is_snapshot:
                            book.bootstrap(
                                SequencedOrderBookSnapshot(
                                    exchange=instrument.exchange,
                                    symbol=instrument.symbol,
                                    market=instrument.market,
                                    sequence=sequence,
                                    timestamp=source_timestamp,
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
                    await self._publish(
                        book, source_timestamp=source_timestamp, force=is_snapshot
                    )
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
