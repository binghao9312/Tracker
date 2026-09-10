import unittest
from time import time_ns

from app.api import DashboardState
from app.models import DerivativeSnapshot, Exchange, UniverseAsset
from app.runtime import LiveRuntime


class Metrics:
    async def append_market(self, _: dict) -> None:
        return None

    async def append_flow(self, _: dict) -> None:
        return None

    async def append_derivative(self, _: dict) -> None:
        return None


def snapshot(timestamp: int, open_interest: float) -> DerivativeSnapshot:
    return DerivativeSnapshot(
        exchange=Exchange.BINANCE,
        symbol="BTCUSDT",
        timestamp=timestamp,
        received_at=time_ns() // 1_000_000,
        open_interest=open_interest,
        open_interest_usd=open_interest * 100,
        funding_rate=None,
        mark_price=100,
    )


class OpenInterestWindowRegressionTests(unittest.TestCase):
    def _runtime(self) -> LiveRuntime:
        return LiveRuntime(
            DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")]),
            Metrics(),
        )

    def test_uses_nearest_reasonable_samples_for_five_fifteen_and_sixty_minutes(self) -> None:
        runtime = self._runtime()
        history = runtime._derivatives[(Exchange.BINANCE, "BTCUSDT")]
        history.extend(
            (
                snapshot(0, 100),
                snapshot(2_700_000, 110),
                snapshot(3_300_000, 120),
                snapshot(3_600_000, 150),
            )
        )

        self.assertAlmostEqual(runtime.oi_change(Exchange.BINANCE, "BTCUSDT", 300), 0.25)
        self.assertAlmostEqual(runtime.oi_change(Exchange.BINANCE, "BTCUSDT", 900), 150 / 110 - 1)
        self.assertAlmostEqual(runtime.oi_change(Exchange.BINANCE, "BTCUSDT", 3600), 0.5)

    def test_insufficient_or_stale_target_history_is_unknown(self) -> None:
        stale = self._runtime()
        stale_history = stale._derivatives[(Exchange.BINANCE, "BTCUSDT")]
        stale_history.extend((snapshot(0, 100), snapshot(3_200_000, 110), snapshot(3_600_000, 120)))
        insufficient = self._runtime()
        insufficient._derivatives[(Exchange.BINANCE, "BTCUSDT")].extend(
            (
                snapshot(3_000_000, 100),
                snapshot(3_600_000, 120),
            )
        )

        self.assertIsNone(stale.oi_change(Exchange.BINANCE, "BTCUSDT", 300))
        self.assertIsNone(insufficient.oi_change(Exchange.BINANCE, "BTCUSDT", 900))


if __name__ == "__main__":
    unittest.main()
