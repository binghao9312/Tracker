import unittest

from app.scoring import ClassificationThresholds, MarketSignal, MoveType, classify_move


class MoveTypeSellPressureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.thresholds = ClassificationThresholds(pressure=2.0, cvd=0.0, oi_change=0.05)

    def test_spot_sell_is_spot_driven(self) -> None:
        signal = MarketSignal("binance", 1.0, 3.0, 0.0, 0.0, -1.0, None, None)
        self.assertEqual(classify_move(signal, self.thresholds), MoveType.SPOT_DRIVEN)

    def test_perp_sell_with_rising_oi_is_leverage_driven(self) -> None:
        signal = MarketSignal("binance", 0.0, 0.0, 1.0, 3.0, None, -1.0, 0.05)
        self.assertEqual(classify_move(signal, self.thresholds), MoveType.LEVERAGE_DRIVEN)

    def test_perp_sell_with_falling_oi_is_leverage_driven(self) -> None:
        signal = MarketSignal("binance", 0.0, 0.0, 1.0, 3.0, None, -1.0, -0.05)
        self.assertEqual(classify_move(signal, self.thresholds), MoveType.LEVERAGE_DRIVEN)

    def test_same_direction_spot_and_perp_sell_is_mixed(self) -> None:
        signal = MarketSignal("binance", 1.0, 3.0, 1.0, 3.0, -1.0, -1.0, 0.05)
        self.assertEqual(classify_move(signal, self.thresholds), MoveType.MIXED)

    def test_same_direction_spot_and_perp_sell_with_falling_oi_is_mixed(self) -> None:
        signal = MarketSignal("binance", 1.0, 3.0, 1.0, 3.0, -1.0, -1.0, -0.05)
        self.assertEqual(classify_move(signal, self.thresholds), MoveType.MIXED)


if __name__ == "__main__":
    unittest.main()
