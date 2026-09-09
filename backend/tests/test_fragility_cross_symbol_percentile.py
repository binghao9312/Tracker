import unittest

from app.liquidity import LiquidityMetrics
from app.scoring import liquidity_fragility_scores


def metrics(depth: float, impact: float, spread: float, capital: float) -> LiquidityMetrics:
    return LiquidityMetrics(
        mid_price=100.0,
        spread_percent=spread,
        bid_depth_0_5=depth / 2,
        ask_depth_0_5=depth / 2,
        bid_depth_1=depth / 2,
        ask_depth_1=depth / 2,
        bid_depth_2=depth / 2,
        ask_depth_2=depth / 2,
        bid_depth_5=depth / 2,
        ask_depth_5=depth / 2,
        buy_impacts={10_000: impact, 50_000: impact},
        sell_impacts={10_000: impact, 50_000: impact},
        capital_to_move_up={1: capital, 2: capital, 5: capital},
        capital_to_move_down={1: capital, 2: capital, 5: capital},
        order_book_imbalance=0.0,
    )


class FragilityCrossSymbolPercentileTests(unittest.TestCase):
    def test_normalizes_venue_aggregates_against_all_active_symbols(self) -> None:
        scores = liquidity_fragility_scores(
            {
                "BTCUSDT": [metrics(300, 0.01, 0.01, 30_000), metrics(100, 0.03, 0.03, 10_000)],
                "ETHUSDT": [metrics(100, 0.05, 0.05, 5_000)],
                "OTHERUSDT": [metrics(20, 0.10, 0.10, 1_000)],
            }
        )

        self.assertEqual(scores["BTCUSDT"], 0.0)
        self.assertEqual(scores["ETHUSDT"], 50.0)
        self.assertEqual(scores["OTHERUSDT"], 100.0)


if __name__ == "__main__":
    unittest.main()
