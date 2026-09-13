"""In-memory order-book state with explicit exchange sequence validation."""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable
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


_RETAINED_LEVELS_PER_SIDE = 1_000
_TRIM_TRIGGER_LEVELS_PER_SIDE = _RETAINED_LEVELS_PER_SIDE * 2


class LocalOrderBook:
    """A RAM-only book that becomes unusable immediately on a sequence gap."""

    def __init__(self) -> None:
        self._snapshot: SequencedOrderBookSnapshot | None = None
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._bid_order: dict[Decimal, float] = {}
        self._ask_order: dict[Decimal, float] = {}
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
        self._bids, self._bid_order = self._levels(snapshot.bids)
        self._asks, self._ask_order = self._levels(snapshot.asks)
        self._trim_books()
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
        self._apply_levels(self._bids, self._bid_order, bids)
        self._apply_levels(self._asks, self._ask_order, asks)
        self._trim_books()
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
        self._apply_levels(self._bids, self._bid_order, bids)
        self._apply_levels(self._asks, self._ask_order, asks)
        self._trim_books()
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
        self._apply_levels(self._bids, self._bid_order, bids)
        self._apply_levels(self._asks, self._ask_order, asks)
        self._trim_books()
        self._sequence = sequence

    def to_model(
        self, timestamp: int, *, received_at: int | None = None, max_levels: int = 200
    ) -> OrderBook:
        snapshot = self._snapshot
        if snapshot is None or not self._synchronized:
            raise RuntimeError("local order book is not synchronized")
        bids = (
            self._top_levels(self._bids, self._bid_order, max_levels, reverse=True)
            if max_levels
            else self.bids
        )
        asks = (
            self._top_levels(self._asks, self._ask_order, max_levels, reverse=False)
            if max_levels
            else self.asks
        )
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
    def _levels(
        levels: Iterable[tuple[Decimal, Decimal]],
    ) -> tuple[dict[Decimal, Decimal], dict[Decimal, float]]:
        book: dict[Decimal, Decimal] = {}
        order: dict[Decimal, float] = {}
        LocalOrderBook._apply_levels(book, order, levels)
        return book, order

    @staticmethod
    def _apply_levels(
        book: dict[Decimal, Decimal],
        order: dict[Decimal, float],
        levels: Iterable[tuple[Decimal, Decimal]],
    ) -> None:
        for price, quantity in levels:
            if price <= 0 or quantity < 0:
                raise ValueError("order-book prices must be positive and quantities non-negative")
            if quantity == 0:
                book.pop(price, None)
                order.pop(price, None)
            else:
                book[price] = quantity
                order[price] = float(price)

    def _trim_books(self) -> None:
        self._trim_book(self._bids, self._bid_order, reverse=True)
        self._trim_book(self._asks, self._ask_order, reverse=False)

    @staticmethod
    def _trim_book(
        book: dict[Decimal, Decimal], order: dict[Decimal, float], *, reverse: bool
    ) -> None:
        if len(book) <= _TRIM_TRIGGER_LEVELS_PER_SIDE:
            return
        retained = LocalOrderBook._top_levels(
            book, order, _RETAINED_LEVELS_PER_SIDE, reverse=reverse
        )
        book.clear()
        book.update(retained)
        order.clear()
        order.update((price, float(price)) for price, _ in retained)

    @staticmethod
    def _top_levels(
        book: dict[Decimal, Decimal],
        order: dict[Decimal, float],
        max_levels: int,
        *,
        reverse: bool,
    ) -> list[tuple[Decimal, Decimal]]:
        select: Callable[..., list[Decimal]] = heapq.nlargest if reverse else heapq.nsmallest
        prices = select(max_levels, book, key=order.__getitem__)
        prices.sort(key=order.__getitem__, reverse=reverse)
        return [(price, book[price]) for price in prices]

    def _require_sync(self) -> int:
        if not self._synchronized or self._sequence is None:
            raise RuntimeError("order book requires a fresh snapshot")
        return self._sequence

    def _invalidate(self, message: str) -> None:
        self._synchronized = False
        raise OrderBookSequenceGap(message)
