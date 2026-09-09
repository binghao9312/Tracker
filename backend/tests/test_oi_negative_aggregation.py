import unittest

from app.api import DashboardState
from app.liquidity import LiquidityMetrics
from app.models import DerivativeSnapshot, Exchange, MarketType, UniverseAsset
from app.runtime import LiveRuntime


class Metrics:
    async def append_market(self, _: dict) -> None:
        return None

    async def append_flow(self, _: dict) -> None:
        return None

    async def append_derivative(self, _: dict) -> None:
        return None


def liquidity() -> LiquidityMetrics:
    return LiquidityMetrics(
        mid_price=100,
        spread_percent=1,
        bid_depth_0_5=100,
        ask_depth_0_5=100,
        bid_depth_1=100,
        ask_depth_1=100,
        bid_depth_2=100,
        ask_depth_2=100,
        bid_depth_5=100,
        ask_depth_5=100,
        buy_impacts={},
        sell_impacts={},
        capital_to_move_up={},
        capital_to_move_down={},
        order_book_imbalance=0,
    )


def snapshot(timestamp: int, interest: float) -> DerivativeSnapshot:
    return DerivativeSnapshot(
        exchange=Exchange.BINANCE,
        symbol="BTCUSDT",
        timestamp=timestamp,
        open_interest=interest,
        open_interest_usd=interest * 100,
        funding_rate=None,
        mark_price=100,
    )


class NegativeOpenInterestAggregationTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_detail_ignores_missing_exchange_and_preserves_signed_largest_move(
        self,
    ) -> None:
        state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        runtime = LiveRuntime(state, Metrics())
        history = runtime._derivatives[(Exchange.BINANCE, "BTCUSDT")]
        history.extend(
            (
                snapshot(0, 100),
                snapshot(2_700_000, 100),
                snapshot(3_300_000, 100),
                snapshot(3_600_000, 80),
            )
        )

        detail = await runtime._build_detail(
            "BTCUSDT",
            {(Exchange.BINANCE, MarketType.PERP): liquidity()},
            0,
        )

        self.assertEqual(
            detail["oi_change_by_exchange"],
            {"binance": {"1m": None, "5m": -0.2, "15m": -0.2, "1h": -0.2}},
        )
        self.assertEqual(
            (detail["oi_change_5m"], detail["oi_change_15m"], detail["oi_change_1h"]),
            (-0.2, -0.2, -0.2),
        )


if __name__ == "__main__":
    unittest.main()
