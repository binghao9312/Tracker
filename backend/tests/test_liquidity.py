import unittest

from app.liquidity import calculate_liquidity
from app.models import Exchange, MarketType, OrderBook, PriceLevel


class LiquidityMetricTests(unittest.TestCase):
    def test_calculates_depth_impact_obi_and_capital(self) -> None:
        book = OrderBook(
            exchange=Exchange.BINANCE,
            symbol="BTCUSDT",
            market=MarketType.SPOT,
            timestamp=1,
            bids=[PriceLevel(price=99, quantity=100), PriceLevel(price=98, quantity=100)],
            asks=[PriceLevel(price=101, quantity=100), PriceLevel(price=102, quantity=100)],
        )

        metrics = calculate_liquidity(book)

        self.assertEqual(metrics.mid_price, 100)
        self.assertEqual(metrics.spread_percent, 2)
        self.assertEqual(metrics.bid_depth_1, 9900)
        self.assertEqual(metrics.ask_depth_1, 10100)
        self.assertAlmostEqual(metrics.buy_impacts[1_000], 0.01)
        self.assertAlmostEqual(metrics.sell_impacts[1_000], -0.01)
        self.assertEqual(metrics.capital_to_move_up[1], 10100)
        self.assertEqual(metrics.capital_to_move_down[1], 9900)
        self.assertAlmostEqual(metrics.order_book_imbalance, -600 / 40000)

    def test_marks_unfillable_market_orders_and_targets(self) -> None:
        book = OrderBook(
            exchange=Exchange.OKX,
            symbol="BTCUSDT",
            market=MarketType.PERP,
            timestamp=1,
            bids=[PriceLevel(price=99, quantity=1)],
            asks=[PriceLevel(price=101, quantity=1)],
        )

        metrics = calculate_liquidity(book)

        self.assertIsNone(metrics.buy_impacts[5_000])
        self.assertIsNone(metrics.capital_to_move_up[2])

    def test_exactly_fillable_float_notional_is_not_reported_unavailable(self) -> None:
        levels = [PriceLevel(price=1.0, quantity=0.1) for _ in range(10)]
        book = OrderBook(
            exchange=Exchange.BINANCE,
            symbol="BTCUSDT",
            market=MarketType.SPOT,
            timestamp=1,
            bids=levels,
            asks=levels,
        )

        metrics = calculate_liquidity(book, impact_notionals=(1,))

        self.assertAlmostEqual(metrics.buy_impacts[1], 0.0)


if __name__ == "__main__":
    unittest.main()
