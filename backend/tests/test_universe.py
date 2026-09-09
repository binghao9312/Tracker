import unittest
from pathlib import Path

from app.universe import JsonMarketUniverseProvider


class UniverseProviderTests(unittest.TestCase):
    def test_loads_ranked_top_fifty_with_bitcoin(self) -> None:
        provider = JsonMarketUniverseProvider(
            Path(__file__).parents[2] / "config" / "universe.json"
        )

        universe = provider.load()

        self.assertEqual(len(universe), 50)
        self.assertEqual(universe[0].symbol, "BTC")
        self.assertEqual(universe[0].rank, 1)
        self.assertEqual(universe[-1].rank, 50)


if __name__ == "__main__":
    unittest.main()
