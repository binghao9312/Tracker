import unittest

from app.paper_trading import FillUnavailable, simulate_market_fill


class PaperFillTests(unittest.TestCase):
    def test_buy_consumes_asks_and_includes_spread_slippage(self) -> None:
        fill = simulate_market_fill([[101, 5], [102, 5]], side="BUY", notional_usdt=1015)
        self.assertEqual(fill.quantity, 10)
        self.assertEqual(fill.vwap, 101.5)
        self.assertGreater(fill.slippage, 0)

    def test_missing_depth_is_an_explicit_skip(self) -> None:
        with self.assertRaisesRegex(FillUnavailable, "INSUFFICIENT_BOOK_DEPTH"):
            simulate_market_fill([[101, 1]], side="BUY", notional_usdt=1_000)


if __name__ == "__main__":
    unittest.main()
