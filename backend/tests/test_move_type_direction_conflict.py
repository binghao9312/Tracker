import unittest

from app.scoring import ClassificationThresholds, MarketSignal, MoveType, classify_move


class MoveTypeDirectionConflictTests(unittest.TestCase):
    def test_spot_buy_and_perp_sell_are_not_mixed(self) -> None:
        thresholds = ClassificationThresholds(pressure=2.0, cvd=0.0, oi_change=0.05)
        signal = MarketSignal("binance", 3.0, 1.0, 1.0, 3.0, 1.0, -1.0, 0.05)

        self.assertEqual(classify_move(signal, thresholds), MoveType.NEUTRAL)


if __name__ == "__main__":
    unittest.main()
