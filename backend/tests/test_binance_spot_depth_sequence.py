import unittest
from decimal import Decimal

from app.models import Exchange, MarketType
from app.orderbook import LocalOrderBook, SequencedOrderBookSnapshot


class BinanceSpotDepthSequenceTests(unittest.TestCase):
    def test_three_multi_update_events_apply_without_pu_or_resync(self) -> None:
        book = LocalOrderBook()
        book.bootstrap(
            SequencedOrderBookSnapshot(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                market=MarketType.SPOT,
                sequence=100,
                timestamp=1,
                bids=[(Decimal("99"), Decimal("1"))],
                asks=[(Decimal("101"), Decimal("1"))],
            )
        )

        for first_sequence, final_sequence in ((101, 103), (104, 107), (108, 110)):
            book.apply_binance_spot_update(
                first_sequence=first_sequence,
                final_sequence=final_sequence,
                bids=[(Decimal("99"), Decimal(final_sequence))],
                asks=[],
            )

        self.assertTrue(book.synchronized)
        self.assertEqual(book.sequence, 110)
        self.assertEqual(book.bids, [(Decimal("99"), Decimal("110"))])


if __name__ == "__main__":
    unittest.main()
