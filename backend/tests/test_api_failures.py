import unittest

from app.exchanges.binance import BinanceAdapter
from app.exchanges.okx import OkxAdapter


class InvalidResponseClient:
    async def get_json(self, _: str) -> object:
        return {"unexpected": "shape"}


class ApiFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_adapters_reject_invalid_responses(self) -> None:
        with self.assertRaises(ValueError):
            await BinanceAdapter(InvalidResponseClient()).discover_markets()
        with self.assertRaises(ValueError):
            await OkxAdapter(InvalidResponseClient()).discover_markets()


if __name__ == "__main__":
    unittest.main()
