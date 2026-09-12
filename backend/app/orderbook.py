"""In-memory order-book state with explicit exchange sequence validation."""

from __future__ import annotations

import heapq
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.models import Exchange, MarketType, OrderBook, PriceLevel


class OrderBookSequenceGap(RuntimeError):
    """An update cannot be safely applied to the local book."""


@dataclass(frozen=True)
class SequencedOrderBookSnapshot:
    exchange: Exchange
    symbol: str
    market: MarketType
    sequence: int
    timestamp: int
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]


class LocalOrderBook:
    """A RAM-only book that becomes unusable immediately on a sequence gap."""

    _MAX_RETAINED_LEVELS = 1_000

    def __init__(self) -> None:
        self._snapshot: SequencedOrderBookSnapshot | None = None
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._bid_heap: list[Decimal] = []
        self._ask_heap: list[Decimal] = []
        self._sequence: int | None = None
        self._synchronized = False

    @property
    def synchronized(self) -> bool:
        return self._synchronized

    @property
    def sequence(self) -> int | None:
        return self._sequence

    @property
    def symbol(self) -> str:
        if self._snapshot is None:
            raise RuntimeError("order book is not bootstrapped")
        return self._snapshot.symbol

    def bootstrap(self, snapshot: SequencedOrderBookSnapshot) -> None:
        self._snapshot = snapshot
        self._bids = self._levels(snapshot.bids)
        self._asks = self._levels(snapshot.asks)
        self._bid_heap = list(self._bids)
        self._ask_heap = [-price for price in self._asks]
        heapq.heapify(self._bid_heap)
        heapq.heapify(self._ask_heap)
        self._trim_side(self._bids, self._bid_heap, asks=False)
        self._trim_side(self._asks, self._ask_heap, asks=True)
        self._sequence = snapshot.sequence
        self._synchronized = True

    def apply_binance_spot_update(
        self,
        *,
        first_sequence: int,
        final_sequence: int,
        bids: Iterable[tuple[Decimal, Decimal]],
        asks: Iterable[tuple[Decimal, Decimal]],
    ) -> None:
        current_sequence = self._require_sync()
        if final_sequence <= current_sequence:
            return
        if first_sequence > current_sequence + 1:
            self._invalidate("Binance Spot depth sequence gap")
        self._apply_side_levels(self._bids, self._bid_heap, bids, asks=False)
        self._apply_side_levels(self._asks, self._ask_heap, asks, asks=True)
        self._sequence = final_sequence

    def apply_binance_futures_update(
        self,
        *,
        first_sequence: int,
        final_sequence: int,
        previous_final_sequence: int | None,
        bids: Iterable[tuple[Decimal, Decimal]],
        asks: Iterable[tuple[Decimal, Decimal]],
    ) -> None:
        current_sequence = self._require_sync()
        is_first_increment = previous_final_sequence is None
        valid = (
            first_sequence <= current_sequence <= final_sequence
            if is_first_increment
            else previous_final_sequence == current_sequence
        )
        if not valid:
            self._invalidate("Binance Futures depth sequence gap")
        self._apply_side_levels(self._bids, self._bid_heap, bids, asks=False)
        self._apply_side_levels(self._asks, self._ask_heap, asks, asks=True)
        self._sequence = final_sequence

    def apply_okx_update(
        self,
        *,
        sequence: int,
        previous_sequence: int,
        bids: Iterable[tuple[Decimal, Decimal]],
        asks: Iterable[tuple[Decimal, Decimal]],
    ) -> None:
        if previous_sequence != self._require_sync():
            self._invalidate("OKX depth sequence gap")
        self._apply_side_levels(self._bids, self._bid_heap, bids, asks=False)
        self._apply_side_levels(self._asks, self._ask_heap, asks, asks=True)
        self._sequence = sequence

    def to_model(
        self, timestamp: int, *, received_at: int | None = None, max_levels: int = 200
    ) -> OrderBook:
        snapshot = self._snapshot
        if snapshot is None or not self._synchronized:
            raise RuntimeError("local order book is not synchronized")
        bids = self._top_levels(self._bids, max_levels, reverse=True)
        asks = self._top_levels(self._asks, max_levels, reverse=False)
        return OrderBook(
            exchange=snapshot.exchange,
            symbol=snapshot.symbol,
            market=snapshot.market,
            timestamp=timestamp,
            received_at=received_at,
            bids=[
                PriceLevel(price=float(price), quantity=float(quantity)) for price, quantity in bids
            ],
            asks=[
                PriceLevel(price=float(price), quantity=float(quantity)) for price, quantity in asks
            ],
        )

    @property
    def bids(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self._bids.items(), reverse=True)

    @property
    def asks(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self._asks.items())

    @staticmethod
    def _top_levels(
        book: dict[Decimal, Decimal], max_levels: int, *, reverse: bool
    ) -> list[tuple[Decimal, Decimal]]:
        if max_levels <= 0:
            if max_levels == 0:
                max_levels = len(book)
            else:
                levels = sorted(book.items(), reverse=reverse)
                return levels[:max_levels]
        if reverse:
            return heapq.nlargest(max_levels, book.items(), key=lambda level: level[0])
        return heapq.nsmallest(max_levels, book.items(), key=lambda level: level[0])

    def _apply_side_levels(
        self,
        book: dict[Decimal, Decimal],
        heap: list[Decimal],
        levels: Iterable[tuple[Decimal, Decimal]],
        *,
        asks: bool,
    ) -> None:
        for price, quantity in levels:
            if price <= 0 or quantity < 0:
                raise ValueError("order-book prices must be positive and quantities non-negative")
            if quantity == 0:
                book.pop(price, None)
            else:
                if price not in book:
                    heapq.heappush(heap, -price if asks else price)
                book[price] = quantity
        self._trim_side(book, heap, asks=asks)

    def _trim_side(self, book: dict[Decimal, Decimal], heap: list[Decimal], *, asks: bool) -> None:
        while len(book) > self._MAX_RETAINED_LEVELS:
            priority = heapq.heappop(heap)
            price = -priority if asks else priority
            if price in book:
                del book[price]
        if len(heap) > 2 * max(len(book), 1):
            heap[:] = [-price if asks else price for price in book]
            heapq.heapify(heap)

    @staticmethod
    def _levels(levels: Iterable[tuple[Decimal, Decimal]]) -> dict[Decimal, Decimal]:
        book: dict[Decimal, Decimal] = {}
        LocalOrderBook._apply_levels(book, levels)
        return book

    @staticmethod
    def _apply_levels(
        book: dict[Decimal, Decimal], levels: Iterable[tuple[Decimal, Decimal]]
    ) -> None:
        for price, quantity in levels:
            if price <= 0 or quantity < 0:
                raise ValueError("order-book prices must be positive and quantities non-negative")
            if quantity == 0:
                book.pop(price, None)
            else:
                book[price] = quantity

    def _require_sync(self) -> int:
        if not self._synchronized or self._sequence is None:
            raise RuntimeError("order book requires a fresh snapshot")
        return self._sequence

    def _invalidate(self, message: str) -> None:
        self._synchronized = False
        raise OrderBookSequenceGap(message)
