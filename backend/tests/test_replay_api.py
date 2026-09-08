import asyncio
import unittest
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.api import DashboardState, create_app
from app.models import UniverseAsset


class ReplayMetrics:
    def __init__(self) -> None:
        self.args: tuple | None = None

    async def history_range(self, *args: object, **kwargs: object) -> dict:
        self.args = args, kwargs
        return {"market": [{"timestamp": 1_735_689_600_000, "price": 101.0}], "flow": [], "derivative": []}


class ReplayTrades:
    async def get_trade(self, _: int) -> dict:
        return {
            "id": 1, "symbol": "BTCUSDT", "exchange": "okx", "market": "perp", "status": "CLOSED",
            "opened_at": datetime(2025, 1, 1, tzinfo=UTC).isoformat(), "closed_at": datetime(2025, 1, 1, 0, 1, tzinfo=UTC).isoformat(),
            "signal_snapshot": {}, "exit_snapshot": {},
        }


class ReplayApiRegressionTests(unittest.TestCase):
    def test_replay_filters_history_to_execution_market_and_returns_milliseconds(self) -> None:
        state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        asyncio.run(state.update_symbol("BTCUSDT", {"price": 100.0}))
        history = ReplayMetrics()
        client = TestClient(create_app(state, history_repository=history, paper_repository=ReplayTrades()))

        response = client.get("/api/paper/trades/1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(history.args[1], {"exchange": "okx", "market": "perp"})
        self.assertIsInstance(response.json()["history"]["market"][0]["timestamp"], int)
