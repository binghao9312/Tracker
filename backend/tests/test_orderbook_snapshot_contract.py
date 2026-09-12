"""Behavioural contract for LocalOrderBook.to_model(), plus a growth guard.

A 45s sampled profile of the live backend on 2026-09-12 attributed 73% of all CPU
to two lines:

    45.89%  bids (app/orderbook.py:135)
    27.24%  asks (app/orderbook.py:139)

Both are properties that fully sort the whole Decimal-keyed book on every access,
and `to_model` sorts everything only to slice the top 200. `_publish` throttles to
10Hz per symbol across ~163 pairs, so that runs up to ~1,630 times a second.
Measured cost of the sorting alone, against a one-core budget:

    1,000 levels/side (the Binance snapshot size)  ->   1.2 cores
    2,000                                          ->   2.7 cores
    5,000                                          ->   8.3 cores
   10,000                                          ->  25.3 cores

So it is over budget at the snapshot size before anything degrades. It then gets
worse over time: Binance `@depth@100ms` is a *diff* stream, and a price level only
leaves the local book on an explicit zero-quantity update, which levels far from
the mid rarely receive. The book therefore grows for as long as the process lives.
That is the shape actually observed in production -- BTCUSDT write gaps grew from
13.8s to 278s over 16 hours, ending in a SIGSEGV.

Two things have to change: stop full-sorting to take a fixed number of levels, and
stop the book growing without bound. The second is a real behaviour change, so the
guarantee it must preserve is written down here rather than left to judgement:

    Discarding levels is invisible as long as the retained depth per side stays
    comfortably above `max_levels`. `to_model(max_levels=200)` is the only caller
    (runtime.py and liquidity.py both read the truncated model, never the raw
    book), so keeping on the order of 1,000 levels per side leaves ~800 levels of
    headroom before any discard could reach the published top 200.

`ReferenceBook` below keeps everything and sorts naively. Where it and the real
implementation disagree about the published levels, the real one is wrong.
"""

from __future__ import annotations

import random
import time
import unittest
from decimal import Decimal

from app.models import Exchange, MarketType
from app.orderbook import LocalOrderBook, SequencedOrderBookSnapshot

MAX_LEVELS = 200
# Headroom the implementation must keep per side. Any bound at or above this is
# invisible at MAX_LEVELS; a bound below it could surface in published output.
MIN_RETAINED_PER_SIDE = 1_000


def snapshot(
    bids: list[tuple[Decimal, Decimal]],
    asks: list[tuple[Decimal, Decimal]],
    sequence: int = 1,
) -> SequencedOrderBookSnapshot:
    return SequencedOrderBookSnapshot(
        exchange=Exchange.BINANCE,
        symbol="BTCUSDT",
        market=MarketType.SPOT,
        sequence=sequence,
        timestamp=1_700_000_000_000,
        bids=bids,
        asks=asks,
    )


def d(value: str | float) -> Decimal:
    return Decimal(str(value))


class ReferenceBook:
    """Keeps every level and sorts from scratch. Slow on purpose, and correct."""

    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}

    def apply(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
    ) -> None:
        for book, levels in ((self.bids, bids), (self.asks, asks)):
            for price, quantity in levels:
                if quantity == 0:
                    book.pop(price, None)
                else:
                    book[price] = quantity

    def top(self, max_levels: int = MAX_LEVELS) -> tuple[list[tuple[float, float]], ...]:
        bids = sorted(self.bids.items(), reverse=True)[:max_levels]
        asks = sorted(self.asks.items())[:max_levels]
        return (
            [(float(p), float(q)) for p, q in bids],
            [(float(p), float(q)) for p, q in asks],
        )


def published(book: LocalOrderBook, max_levels: int = MAX_LEVELS):
    model = book.to_model(1_700_000_000_000, max_levels=max_levels)
    return (
        [(level.price, level.quantity) for level in model.bids],
        [(level.price, level.quantity) for level in model.asks],
    )


def _seed_levels(count: int, *, mid: float = 50_000.0) -> tuple[list, list]:
    """A book `count` levels deep per side, one cent apart."""
    bids = [(d(round(mid - 0.01 * (i + 1), 2)), d("1.5")) for i in range(count)]
    asks = [(d(round(mid + 0.01 * (i + 1), 2)), d("1.5")) for i in range(count)]
    return bids, asks


class SnapshotContractTests(unittest.TestCase):
    def test_published_levels_match_the_reference_after_random_updates(self) -> None:
        """Adds, requotes and deletions, mixed near and far from the mid."""
        rnd = random.Random(4)
        real, ref = LocalOrderBook(), ReferenceBook()
        bids, asks = _seed_levels(1_200)
        real.bootstrap(snapshot(bids, asks))
        ref.apply(bids, asks)

        sequence = 1
        for round_index in range(40):
            up_bids, up_asks = [], []
            for _ in range(60):
                # Most churn happens near the mid; some of it far away.
                offset = rnd.randrange(1, 30) if rnd.random() < 0.7 else rnd.randrange(30, 1_500)
                qty = d("0") if rnd.random() < 0.25 else d(str(round(rnd.uniform(0.1, 9), 3)))
                up_bids.append((d(round(50_000 - 0.01 * offset, 2)), qty))
                up_asks.append((d(round(50_000 + 0.01 * offset, 2)), qty))
            sequence += 1
            real.apply_binance_spot_update(
                first_sequence=sequence, final_sequence=sequence, bids=up_bids, asks=up_asks
            )
            ref.apply(up_bids, up_asks)

            got_bids, got_asks = published(real)
            want_bids, want_asks = ref.top()
            self.assertEqual(got_bids, want_bids, f"bids diverged at round {round_index}")
            self.assertEqual(got_asks, want_asks, f"asks diverged at round {round_index}")

    def test_published_bids_descend_and_asks_ascend(self) -> None:
        bids, asks = _seed_levels(500)
        book = LocalOrderBook()
        book.bootstrap(snapshot(bids, asks))
        got_bids, got_asks = published(book)
        self.assertEqual([p for p, _ in got_bids], sorted((p for p, _ in got_bids), reverse=True))
        self.assertEqual([p for p, _ in got_asks], sorted(p for p, _ in got_asks))

    def test_publishes_exactly_max_levels_when_the_book_is_deeper(self) -> None:
        bids, asks = _seed_levels(1_500)
        book = LocalOrderBook()
        book.bootstrap(snapshot(bids, asks))
        got_bids, got_asks = published(book)
        self.assertEqual(len(got_bids), MAX_LEVELS)
        self.assertEqual(len(got_asks), MAX_LEVELS)

    def test_publishes_everything_when_the_book_is_shallower(self) -> None:
        bids, asks = _seed_levels(12)
        book = LocalOrderBook()
        book.bootstrap(snapshot(bids, asks))
        got_bids, got_asks = published(book)
        self.assertEqual(len(got_bids), 12)
        self.assertEqual(len(got_asks), 12)

    def test_zero_quantity_removes_the_level_from_published_output(self) -> None:
        bids, asks = _seed_levels(5)
        book = LocalOrderBook()
        book.bootstrap(snapshot(bids, asks))
        best_bid = published(book)[0][0][0]
        book.apply_binance_spot_update(
            first_sequence=2, final_sequence=2, bids=[(d(best_bid), d("0"))], asks=[]
        )
        self.assertNotIn(best_bid, [p for p, _ in published(book)[0]])

    def test_an_unsynchronized_book_refuses_to_publish(self) -> None:
        book = LocalOrderBook()
        with self.assertRaises(RuntimeError):
            book.to_model(1_700_000_000_000)

    def test_deep_levels_still_reachable_by_asking_for_more(self) -> None:
        """Whatever bound is applied must leave real depth behind MAX_LEVELS."""
        bids, asks = _seed_levels(MIN_RETAINED_PER_SIDE)
        book = LocalOrderBook()
        book.bootstrap(snapshot(bids, asks))
        deep_bids, deep_asks = published(book, max_levels=MIN_RETAINED_PER_SIDE)
        self.assertEqual(len(deep_bids), MIN_RETAINED_PER_SIDE)
        self.assertEqual(len(deep_asks), MIN_RETAINED_PER_SIDE)


class SnapshotGrowthTests(unittest.TestCase):
    """The two properties that failed in production."""

    @staticmethod
    def _accumulate(update_rounds: int) -> LocalOrderBook:
        """Simulate a diff stream that keeps introducing new far-from-mid levels.

        This is the real accumulation pattern: new prices appear and are never
        explicitly zeroed, so nothing ever removes them.
        """
        book = LocalOrderBook()
        bids, asks = _seed_levels(1_000)
        book.bootstrap(snapshot(bids, asks))
        sequence = 1
        for i in range(update_rounds):
            far = 20.0 + i * 0.01
            sequence += 1
            book.apply_binance_spot_update(
                first_sequence=sequence,
                final_sequence=sequence,
                bids=[(d(round(50_000 - far, 2)), d("0.4"))],
                asks=[(d(round(50_000 + far, 2)), d("0.4"))],
            )
        return book

    def test_book_does_not_grow_without_bound(self) -> None:
        book = self._accumulate(40_000)
        held = max(len(book._bids), len(book._asks))
        self.assertLess(
            held,
            20_000,
            msg=(
                f"the book retained {held} levels per side after 40,000 diff updates. "
                "A diff stream never zeroes far levels, so an unbounded book grows for "
                "the life of the process and every publish sorts all of it."
            ),
        )

    def test_publish_cost_does_not_grow_with_accumulated_updates(self) -> None:
        def best_of(b: LocalOrderBook, rounds: int = 5) -> float:
            best = float("inf")
            for _ in range(rounds):
                start = time.perf_counter()
                for _ in range(10):
                    b.to_model(1_700_000_000_000, max_levels=MAX_LEVELS)
                best = min(best, (time.perf_counter() - start) / 10)
            return best

        light = self._accumulate(2_000)
        heavy = self._accumulate(40_000)  # 20x the updates

        best_of(light, rounds=2)  # warm up
        light_cost = best_of(light)
        heavy_cost = best_of(heavy)

        ratio = heavy_cost / light_cost if light_cost > 0 else float("inf")
        self.assertLess(
            ratio,
            3.0,
            msg=(
                f"to_model cost tracks accumulated updates: 20x the updates cost "
                f"{ratio:.1f}x the time ({light_cost * 1e6:.0f}us -> {heavy_cost * 1e6:.0f}us). "
                "Publishing a fixed 200 levels should cost the same regardless of how "
                "long the process has been running."
            ),
        )


if __name__ == "__main__":
    unittest.main()
