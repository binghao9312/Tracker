import unittest

from app.scoring import (
    ClassificationThresholds,
    CrossExchangeState,
    MarketSignal,
    activity_score,
    cross_exchange_state,
)


class ActivitySellPressureTests(unittest.TestCase):
    def test_sell_pressure_contributes_the_same_as_buy_pressure(self) -> None:
        bullish = activity_score(3.0, 3.0, 1.0, 0.0, 0.0, True)
        bearish = activity_score(3.0, 3.0, -1.0, 0.0, 0.0, True)

        self.assertEqual(bearish, bullish)

    def test_cross_exchange_confirms_bearish_sell_pressure_anomaly(self) -> None:
        thresholds = ClassificationThresholds(pressure=2.0, cvd=0.0, oi_change=0.05)
        signals = [
            MarketSignal("binance", 0, 3, 0, 3, -1, -1, 0),
            MarketSignal("okx", 0, 3, 0, 3, -1, -1, 0),
        ]

        self.assertEqual(cross_exchange_state(signals, thresholds), CrossExchangeState.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
