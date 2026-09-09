import unittest

from app.trade_signal import TradeBias, calculate_trade_signal
from tests.paper_helpers import detail


class TradeSignalTests(unittest.TestCase):
    def test_long_requires_pressure_and_volume_imbalance(self) -> None:
        signal = calculate_trade_signal(detail())
        self.assertEqual(signal.bias, TradeBias.LONG)
        self.assertGreaterEqual(signal.delta_ratio, 0.15)

    def test_activity_is_not_a_direction(self) -> None:
        state = detail()
        state["spot"] = {
            "buy_pressure_5m": 1,
            "sell_pressure_5m": 1,
            "buy_volume_5m": 500,
            "sell_volume_5m": 500,
        }
        state["perp"] = {}
        self.assertEqual(calculate_trade_signal(state).bias, TradeBias.NONE)


if __name__ == "__main__":
    unittest.main()
