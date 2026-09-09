import unittest

from app.models import MarketType
from app.symbols import normalize_okx_instrument, normalized_symbol


class SymbolNormalizationTests(unittest.TestCase):
    def test_normalizes_usdt_symbols(self) -> None:
        self.assertEqual(normalized_symbol("btc"), "BTCUSDT")
        self.assertEqual(normalize_okx_instrument("BTC-USDT", MarketType.SPOT), "BTCUSDT")
        self.assertEqual(normalize_okx_instrument("BTC-USDT-SWAP", MarketType.PERP), "BTCUSDT")

    def test_rejects_non_usdt_or_invalid_okx_instruments(self) -> None:
        with self.assertRaises(ValueError):
            normalized_symbol("BTC", "USDC")
        with self.assertRaises(ValueError):
            normalize_okx_instrument("BTC-USDT-SWAP", MarketType.SPOT)
        with self.assertRaises(ValueError):
            normalized_symbol("币安人生")


if __name__ == "__main__":
    unittest.main()
