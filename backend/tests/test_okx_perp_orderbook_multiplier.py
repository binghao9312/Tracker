import unittest
from decimal import Decimal

from app.collectors.orderbooks import _depth_levels
from app.exchanges.okx import OkxAdapter
from app.liquidity import calculate_liquidity
from app.models import Exchange, MarketInstrument, MarketType, OrderBook, PriceLevel
from app.paper_trading import FillUnavailable, simulate_market_fill


class SnapshotHttp:
    async def get_json(self, _: str) -> dict[str, object]:
        return {
            "code": "0",
            "data": [
                {
                    "seqId": "7",
                    "ts": "1000",
                    "bids": [["99", "25"]],
                    "asks": [["100", "25"]],
                }
            ],
        }


class OkxPerpOrderBookMultiplierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.perp = MarketInstrument(
            exchange=Exchange.OKX,
            symbol="BTCUSDT",
            market=MarketType.PERP,
            exchange_symbol="BTC-USDT-SWAP",
            base_quantity_multiplier=0.1,
        )

    async def test_rest_perpetual_depth_is_base_quantity_and_usdt_depth(self) -> None:
        snapshot = await OkxAdapter(SnapshotHttp()).fetch_order_book_snapshot(self.perp)

        self.assertEqual(snapshot.asks, [(Decimal("100"), Decimal("2.5"))])
        book = OrderBook(
            exchange=Exchange.OKX,
            symbol="BTCUSDT",
            market=MarketType.PERP,
            timestamp=1,
            bids=[PriceLevel(price=99, quantity=2.5)],
            asks=[PriceLevel(price=100, quantity=2.5)],
        )
        self.assertEqual(calculate_liquidity(book).ask_depth_2, 250)

    def test_websocket_increment_normalizes_contract_quantity_once_and_spot_does_not(self) -> None:
        raw = [["100", "25"]]

        self.assertEqual(
            _depth_levels(raw, quantity_multiplier=self.perp.base_quantity_multiplier),
            [(Decimal("100"), Decimal("2.5"))],
        )
        self.assertEqual(_depth_levels(raw), [(Decimal("100"), Decimal("25"))])

    async def test_normalized_perpetual_book_fill_cannot_use_contract_count_as_base_depth(
        self,
    ) -> None:
        snapshot = await OkxAdapter(SnapshotHttp()).fetch_order_book_snapshot(self.perp)
        normalized_asks = [[float(price), float(quantity)] for price, quantity in snapshot.asks]

        fill = simulate_market_fill(normalized_asks, side="BUY", quantity=2.5)
        self.assertEqual((fill.quantity, fill.quote_notional), (2.5, 250))
        with self.assertRaisesRegex(FillUnavailable, "INSUFFICIENT_BOOK_DEPTH"):
            simulate_market_fill(normalized_asks, side="BUY", quantity=25)


if __name__ == "__main__":
    unittest.main()
