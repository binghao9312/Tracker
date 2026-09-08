import unittest

from app.derivatives import FundingState, OpenInterestHistory, funding_state
from app.models import DerivativeSnapshot, Exchange


def snapshot(timestamp: int, open_interest: float) -> DerivativeSnapshot:
    return DerivativeSnapshot(
        exchange=Exchange.BINANCE,
        symbol="BTCUSDT",
        timestamp=timestamp,
        open_interest=open_interest,
        open_interest_usd=open_interest * 100,
        funding_rate=0.0002,
        mark_price=100,
    )


class DerivativeMetricTests(unittest.TestCase):
    def test_calculates_historical_open_interest_changes(self) -> None:
        history = OpenInterestHistory()
        history.add(snapshot(0, 100))
        history.add(snapshot(60_000, 110))
        history.add(snapshot(300_000, 150))

        metrics = history.metrics()

        self.assertAlmostEqual(metrics.oi_change_1m, 150 / 110 - 1)
        self.assertAlmostEqual(metrics.oi_change_5m, 0.5)
        self.assertIsNone(metrics.oi_change_15m)

    def test_classifies_funding_from_configured_thresholds(self) -> None:
        self.assertEqual(funding_state(0.0002, 0.001, -0.001), FundingState.NORMAL)
        self.assertEqual(funding_state(0.002, 0.001, -0.001), FundingState.ELEVATED_POSITIVE)
        self.assertEqual(funding_state(-0.002, 0.001, -0.001), FundingState.ELEVATED_NEGATIVE)


if __name__ == "__main__":
    unittest.main()
