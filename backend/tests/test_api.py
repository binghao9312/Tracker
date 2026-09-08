import asyncio
import unittest

from fastapi.testclient import TestClient

from app.api import DashboardState, create_app
from app.models import UniverseAsset


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = DashboardState([UniverseAsset(rank=1, symbol="BTC", name="Bitcoin")])
        self.client = TestClient(create_app(self.state))

    def test_exposes_universe_and_sorted_scanner(self) -> None:
        asyncio.run(
            self.state.update_symbol(
                "BTCUSDT",
                {"price": 100_000, "activity_score": 75, "liquidity_fragility": 30},
            )
        )

        universe = self.client.get("/api/universe")
        scanner = self.client.get("/api/scanner")
        detail = self.client.get("/api/symbol/BTCUSDT")

        self.assertEqual(universe.status_code, 200)
        self.assertEqual(universe.json()[0]["symbol"], "BTC")
        self.assertEqual(scanner.json()[0]["activity_score"], 75)
        self.assertEqual(detail.json()["price"], 100_000)

    def test_rejects_unknown_symbol(self) -> None:
        response = self.client.get("/api/symbol/UNKNOWNUSDT")
        self.assertEqual(response.status_code, 404)

    def test_streams_initial_scanner_and_symbol_snapshots(self) -> None:
        asyncio.run(
            self.state.update_symbol(
                "BTCUSDT",
                {"price": 100_000, "activity_score": 75, "liquidity_fragility": 30},
            )
        )

        with self.client.websocket_connect("/ws/scanner") as scanner_socket:
            scanner_message = scanner_socket.receive_json()
        with self.client.websocket_connect("/ws/symbol/BTCUSDT") as symbol_socket:
            symbol_message = symbol_socket.receive_json()

        self.assertEqual(scanner_message["data"][0]["symbol"], "BTCUSDT")
        self.assertEqual(symbol_message["data"]["price"], 100_000)


if __name__ == "__main__":
    unittest.main()
