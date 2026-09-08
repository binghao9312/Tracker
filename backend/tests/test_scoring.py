import unittest

from app.scoring import (
    ClassificationThresholds,
    CrossExchangeState,
    MarketSignal,
    MoveType,
    activity_score,
    classify_move,
    cross_exchange_state,
    liquidity_fragility_score,
)


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.thresholds = ClassificationThresholds(pressure=2, cvd=10, oi_change=0.05)

    def test_classifies_spot_perp_and_mixed_signals(self) -> None:
        signal = MarketSignal("binance", 3, 3, 20, 20, 0.1)
        self.assertEqual(classify_move(signal, self.thresholds), MoveType.MIXED)
        self.assertEqual(
            classify_move(MarketSignal("okx", 3, 1, 20, 0, 0), self.thresholds),
            MoveType.SPOT_DRIVEN,
        )

    def test_compares_exchange_confirmation_and_scores_independently(self) -> None:
        signals = [
            MarketSignal("binance", 3, 0, 0, 0, 0),
            MarketSignal("okx", 3, 0, 0, 0, 0),
        ]
        self.assertEqual(cross_exchange_state(signals, self.thresholds), CrossExchangeState.CONFIRMED)
        self.assertEqual(
            liquidity_fragility_score(
                depth_2=[100, 10], impact_10k=[0.01, 0.1], impact_50k=[0.02, 0.2],
                spread=[0.01, 0.1], capital_to_move_2=[1000, 100], index=1
            ),
            100,
        )
        self.assertGreater(activity_score(3, 3, 1, 1, 0.001, True), 0)


if __name__ == "__main__":
    unittest.main()
