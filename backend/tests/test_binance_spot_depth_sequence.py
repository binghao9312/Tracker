import unittest
from decimal import Decimal

from app.models import Exchange, MarketType
from app.orderbook import LocalOrderBook, OrderBookSequenceGap, SequencedOrderBookSnapshot


class BinanceSpotDepthSequenceTests(unittest.TestCase):
    @staticmethod
    def _book(sequence: int = 100) -> LocalOrderBook:
        book = LocalOrderBook()
        book.bootstrap(
            SequencedOrderBookSnapshot(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                market=MarketType.SPOT,
                sequence=sequence,
                timestamp=1,
                bids=[(Decimal("99"), Decimal("1"))],
                asks=[(Decimal("101"), Decimal("1"))],
            )
        )
        return book

    def test_three_multi_update_events_apply_without_resync(self) -> None:
        book = self._book()

        for first_sequence, final_sequence in ((101, 103), (104, 107), (108, 110)):
            book.apply_binance_spot_update(
                first_sequence=first_sequence,
                final_sequence=final_sequence,
                bids=[(Decimal("99"), Decimal(final_sequence))],
                asks=[],
            )

        self.assertTrue(book.synchronized)
        self.assertEqual(book.sequence, 110)

    def test_overlapping_updates_apply_new_sequence_range(self) -> None:
        book = self._book()

        for first_sequence, final_sequence in ((99, 104), (103, 108), (108, 112)):
            book.apply_binance_spot_update(
                first_sequence=first_sequence,
                final_sequence=final_sequence,
                bids=[(Decimal("99"), Decimal(final_sequence))],
                asks=[],
            )

        self.assertTrue(book.synchronized)
        self.assertEqual(book.sequence, 112)
        self.assertEqual(book.bids, [(Decimal("99"), Decimal("112"))])

    def test_stale_update_is_ignored(self) -> None:
        book = self._book(sequence=110)

        book.apply_binance_spot_update(
            first_sequence=100,
            final_sequence=105,
            bids=[(Decimal("99"), Decimal("9"))],
            asks=[],
        )

        self.assertTrue(book.synchronized)
        self.assertEqual(book.sequence, 110)
        self.assertEqual(book.bids, [(Decimal("99"), Decimal("1"))])

    def test_gap_invalidates_the_book(self) -> None:
        book = self._book(sequence=110)

        with self.assertRaises(OrderBookSequenceGap):
            book.apply_binance_spot_update(
                first_sequence=115,
                final_sequence=118,
                bids=[],
                asks=[],
            )

        self.assertFalse(book.synchronized)



class BinanceFuturesDepthSequenceTests(unittest.TestCase):
    @staticmethod
    def _book(sequence: int = 100) -> LocalOrderBook:
        book = LocalOrderBook()
        book.bootstrap(
            SequencedOrderBookSnapshot(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                market=MarketType.PERP,
                sequence=sequence,
                timestamp=1,
                bids=[(Decimal("99"), Decimal("1"))],
                asks=[(Decimal("101"), Decimal("1"))],
            )
        )
        return book

    def test_initial_update_must_contain_snapshot_sequence(self) -> None:
        cases = (
            ("first_equals_snapshot", 100, 101, True),
            ("final_equals_snapshot", 99, 100, True),
            ("overlaps_snapshot", 95, 105, True),
            ("starts_after_snapshot", 101, 102, False),
        )
        for name, first_sequence, final_sequence, valid in cases:
            with self.subTest(name=name):
                book = self._book()
                if valid:
                    book.apply_binance_futures_update(
                        first_sequence=first_sequence,
                        final_sequence=final_sequence,
                        previous_final_sequence=None,
                        bids=[],
                        asks=[],
                    )
                    self.assertTrue(book.synchronized)
                    self.assertEqual(book.sequence, final_sequence)
                else:
                    with self.assertRaises(OrderBookSequenceGap):
                        book.apply_binance_futures_update(
                            first_sequence=first_sequence,
                            final_sequence=final_sequence,
                            previous_final_sequence=None,
                            bids=[],
                            asks=[],
                        )
                    self.assertFalse(book.synchronized)

    def test_later_update_requires_previous_final_sequence_match(self) -> None:
        book = self._book()
        book.apply_binance_futures_update(
            first_sequence=100,
            final_sequence=102,
            previous_final_sequence=None,
            bids=[],
            asks=[],
        )
        book.apply_binance_futures_update(
            first_sequence=103,
            final_sequence=104,
            previous_final_sequence=102,
            bids=[],
            asks=[],
        )

        with self.assertRaises(OrderBookSequenceGap):
            book.apply_binance_futures_update(
                first_sequence=105,
                final_sequence=106,
                previous_final_sequence=103,
                bids=[],
                asks=[],
            )

        self.assertFalse(book.synchronized)

if __name__ == "__main__":
    unittest.main()
