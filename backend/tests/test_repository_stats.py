import unittest
from decimal import Decimal

from app.repository import PaperTradeRepository


class StaticTradeRepository(PaperTradeRepository):
    def __init__(self, trades: list[dict]) -> None:
        self._trades = trades

    async def list_trades(self, *args: object, **kwargs: object) -> list[dict]:
        return self._trades


class RepositoryStatsRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_are_finite_and_boundary_buckets_do_not_overlap(self) -> None:
        closed = [
            {
                "status": "CLOSED",
                "net_pnl": Decimal("1"),
                "gross_pnl": 1.0,
                "return_pct": Decimal("0.01"),
                "entry_activity_score": Decimal("85"),
                "entry_liquidity_fragility": Decimal("25"),
                "side": "LONG",
                "entry_move_type": "MIXED",
                "entry_cross_exchange_state": "CONFIRMED",
                "holding_seconds": 1.0,
                "max_favorable_excursion_pct": 0.1,
                "max_adverse_excursion_pct": -0.1,
            },
            {
                "status": "CLOSED",
                "net_pnl": float("nan"),
                "gross_pnl": float("inf"),
                "return_pct": float("nan"),
                "entry_activity_score": Decimal("100"),
                "entry_liquidity_fragility": Decimal("100"),
                "side": "SHORT",
                "entry_move_type": "MIXED",
                "entry_cross_exchange_state": "CONFIRMED",
                "holding_seconds": 1.0,
                "max_favorable_excursion_pct": 0.1,
                "max_adverse_excursion_pct": -0.1,
            },
        ]
        stats = await StaticTradeRepository(closed).stats()
        self.assertIsNone(stats["profit_factor"])
        self.assertEqual(stats["breakdowns"]["activity_score"]["80-85"]["trades"], 0)
        self.assertEqual(stats["breakdowns"]["activity_score"]["85-90"]["trades"], 1)
        self.assertEqual(stats["breakdowns"]["activity_score"]["95-100"]["trades"], 1)
        self.assertEqual(stats["breakdowns"]["liquidity_fragility"]["25-50"]["trades"], 1)
        self.assertEqual(stats["breakdowns"]["liquidity_fragility"]["75-100"]["trades"], 1)
        self.assertEqual(stats["net_pnl"], 1.0)
