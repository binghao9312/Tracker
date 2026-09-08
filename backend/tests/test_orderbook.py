import unittest
from decimal import Decimal

from app.models import Exchange, MarketType
from app.orderbook import LocalOrderBook, OrderBookSequenceGap, SequencedOrderBookSnapshot


class LocalOrderBookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = LocalOrderBook()
        self.book.bootstrap(
            SequencedOrderBookSnapshot(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                market=MarketType.SPOT,
                sequence=10,
                timestamp=1,
                bids=[(Decimal("99"), Decimal("1"))],
                asks=[(Decimal("101"), Decimal("1"))],
            )
        )

    def test_applies_contiguous_binance_updates(self) -> None:
        self.book.apply_binance_update(
            first_sequence=11,
            final_sequence=11,
            previous_final_sequence=None,
            bids=[(Decimal("99"), Decimal("2"))],
            asks=[],
        )
        self.book.apply_binance_update(
            first_sequence=12,
            final_sequence=12,
            previous_final_sequence=11,
            bids=[],
            asks=[(Decimal("101"), Decimal("0"))],
        )

        self.assertEqual(self.book.sequence, 12)
        self.assertTrue(self.book.synchronized)
        self.assertEqual(self.book.bids, [(Decimal("99"), Decimal("2"))])
        self.assertEqual(self.book.asks, [])

    def test_gap_invalidates_book_until_new_snapshot(self) -> None:
        with self.assertRaises(OrderBookSequenceGap):
            self.book.apply_binance_update(
                first_sequence=12,
                final_sequence=12,
                previous_final_sequence=None,
                bids=[],
                asks=[],
            )

        self.assertFalse(self.book.synchronized)
        with self.assertRaises(RuntimeError):
            self.book.to_model(timestamp=2)
        self.book.bootstrap(
            SequencedOrderBookSnapshot(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                market=MarketType.SPOT,
                sequence=20,
                timestamp=3,
                bids=[(Decimal("99"), Decimal("1"))],
                asks=[(Decimal("101"), Decimal("1"))],
            )
        )
        self.book.apply_binance_update(
            first_sequence=21,
            final_sequence=21,
            previous_final_sequence=None,
            bids=[],
            asks=[],
        )
        self.assertTrue(self.book.synchronized)


if __name__ == "__main__":
    unittest.main()
