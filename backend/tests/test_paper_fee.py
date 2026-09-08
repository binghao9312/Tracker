import unittest

from app.paper_trading import simulate_market_fill


class PaperFeeTests(unittest.TestCase):
    def test_round_trip_fee_uses_each_executed_notional(self) -> None:
        entry = simulate_market_fill([[101, 20]], side="BUY", notional_usdt=1_010)
        exit_fill = simulate_market_fill([[99, 20]], side="SELL", quantity=entry.quantity)
        fees = (entry.quote_notional + exit_fill.quote_notional) * 5 / 10_000
        self.assertEqual(fees, 1.0)


if __name__ == "__main__":
    unittest.main()
