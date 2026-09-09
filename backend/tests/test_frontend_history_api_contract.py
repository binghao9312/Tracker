import asyncio
import unittest

from fastapi.testclient import TestClient

from app.api import DashboardState, create_app
from app.models import UniverseAsset


class HistoryRepository:
    async def history(self, symbol: str) -> dict[str, list[dict[str, object]]]:
        if symbol != "BTCUSDT":
            raise AssertionError("history must use the monitored normalized symbol")
        return {
            "market": [{"timestamp": 1, "price": 100.0}],
            "flow": [{"timestamp": 1, "cvd_5m": 10.0}],
            "derivative": [{"timestamp": 1, "oi_change_5m": -0.1}],
        }


class FrontendHistoryApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        asyncio.run(
            self.state.update_symbol(
                "BTCUSDT",
                {
                    "price": 100.0,
                    "activity_score": 55.0,
                    "liquidity_fragility": 20.0,
                    "move_type": "MIXED",
                    "cross_exchange_state": "DIVERGENT",
                    "oi_change_5m": -0.1,
                    "funding": 0.0001,
                    "spot": {
                        "buy_pressure_1m": 2.0,
                        "buy_pressure_5m": 3.0,
                        "sell_pressure_1m": 1.0,
                        "sell_pressure_5m": 1.5,
                        "cvd_5m": 10.0,
                    },
                    "perp": {
                        "buy_pressure_1m": 1.0,
                        "buy_pressure_5m": 1.5,
                        "sell_pressure_1m": 2.0,
                        "sell_pressure_5m": 2.5,
                        "cvd_5m": -8.0,
                    },
                },
            )
        )
        self.client = TestClient(create_app(self.state, history_repository=HistoryRepository()))

    def test_history_and_scanner_http_websocket_share_expanded_row_contract(self) -> None:
        history = self.client.get("/api/symbol/BTCUSDT/history")
        scanner = self.client.get("/api/scanner")
        with self.client.websocket_connect("/ws/scanner") as socket:
            websocket = socket.receive_json()

        self.assertEqual(history.status_code, 200)
        self.assertEqual(set(history.json()), {"market", "flow", "derivative"})
        self.assertEqual(history.json()["derivative"][0]["oi_change_5m"], -0.1)
        expected = {
            "symbol",
            "market_cap_rank",
            "price",
            "liquidity_fragility",
            "activity_score",
            "buy_pressure_1m",
            "buy_pressure_5m",
            "sell_pressure_1m",
            "sell_pressure_5m",
            "spot_cvd_5m",
            "perp_cvd_5m",
            "oi_change_5m",
            "funding",
            "move_type",
            "cross_exchange_state",
        }
        self.assertEqual(set(scanner.json()[0]), expected)
        self.assertEqual(websocket["type"], "scanner")
        self.assertEqual(websocket["data"], scanner.json())


if __name__ == "__main__":
    unittest.main()
