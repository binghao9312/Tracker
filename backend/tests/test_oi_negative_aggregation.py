import unittest
from time import time_ns

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
        received_at=time_ns() // 1_000_000,
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


    async def test_stale_binance_derivative_is_hidden_while_fresh_okx_remains_usable(self) -> None:
        state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        runtime = LiveRuntime(state, Metrics())
        now_ms = time_ns() // 1_000_000
        await runtime.on_derivative(
            DerivativeSnapshot(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                timestamp=now_ms - 61_000,
                received_at=now_ms - 61_000,
                open_interest=100,
                open_interest_usd=10_000,
                funding_rate=0.01,
                mark_price=100,
            )
        )
        await runtime.on_derivative(
            DerivativeSnapshot(
                exchange=Exchange.OKX,
                symbol="BTCUSDT",
                timestamp=now_ms,
                received_at=now_ms,
                open_interest=200,
                open_interest_usd=20_000,
                funding_rate=0.02,
                mark_price=100,
            )
        )

        detail = await runtime._build_detail(
            "BTCUSDT",
            {(Exchange.OKX, MarketType.PERP): liquidity()},
            0,
        )

        self.assertIsNone(detail["derivatives"]["binance"]["open_interest"])
        self.assertEqual(detail["derivatives"]["okx"]["open_interest"], 200)
        self.assertEqual(detail["funding"], 0.02)

    async def test_missing_receive_time_ages_from_source_timestamp(self) -> None:
        runtime = LiveRuntime(DashboardState([]), Metrics())
        now_ms = time_ns() // 1_000_000
        await runtime.on_derivative(
            DerivativeSnapshot(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                timestamp=now_ms - 61_000,
                open_interest=100,
                open_interest_usd=10_000,
                funding_rate=0.01,
                mark_price=100,
            )
        )

        detail = await runtime._build_detail(
            "BTCUSDT",
            {(Exchange.BINANCE, MarketType.PERP): liquidity()},
            0,
        )

        self.assertIsNone(detail["derivatives"]["binance"]["open_interest"])

if __name__ == "__main__":
    unittest.main()
