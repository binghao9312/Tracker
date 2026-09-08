import unittest

from app.exchanges.binance import PERP_EXCHANGE_INFO_URL, SPOT_EXCHANGE_INFO_URL, BinanceAdapter
from app.exchanges.okx import SPOT_INSTRUMENTS_URL, SWAP_INSTRUMENTS_URL, OkxAdapter
from app.models import Exchange, MarketInstrument, MarketType


class FakeHttpClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses

    async def get_json(self, url: str) -> object:
        return self._responses[url]


class ExchangeDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_binance_keeps_only_trading_usdt_spot_and_perpetuals(self) -> None:
        adapter = BinanceAdapter(
            FakeHttpClient(
                {
                    SPOT_EXCHANGE_INFO_URL: {
                        "symbols": [
                            {
                                "symbol": "BTCUSDT",
                                "baseAsset": "BTC",
                                "quoteAsset": "USDT",
                                "status": "TRADING",
                                "isSpotTradingAllowed": True,
                            },
                            {
                                "symbol": "ETHUSDC",
                                "baseAsset": "ETH",
                                "quoteAsset": "USDC",
                                "status": "TRADING",
                                "isSpotTradingAllowed": True,
                            },
                        ]
                    },
                    PERP_EXCHANGE_INFO_URL: {
                        "symbols": [
                            {
                                "symbol": "BTCUSDT",
                                "baseAsset": "BTC",
                                "quoteAsset": "USDT",
                                "status": "TRADING",
                                "contractType": "PERPETUAL",
                            },
                            {
                                "symbol": "ETHUSDT_260627",
                                "baseAsset": "ETH",
                                "quoteAsset": "USDT",
                                "status": "TRADING",
                                "contractType": "CURRENT_QUARTER",
                            },
                        ]
                    },
                }
            )
        )

        markets = await adapter.discover_markets()

        self.assertEqual(
            [(market.symbol, market.market) for market in markets],
            [("BTCUSDT", MarketType.SPOT), ("BTCUSDT", MarketType.PERP)],
        )
        self.assertTrue(all(market.exchange is Exchange.BINANCE for market in markets))

    async def test_okx_keeps_only_live_linear_usdt_markets(self) -> None:
        adapter = OkxAdapter(
            FakeHttpClient(
                {
                    SPOT_INSTRUMENTS_URL: {
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT",
                                "instType": "SPOT",
                                "state": "live",
                                "quoteCcy": "USDT",
                            },
                            {
                                "instId": "BTC-USDC",
                                "instType": "SPOT",
                                "state": "live",
                                "quoteCcy": "USDC",
                            },
                        ],
                    },
                    SWAP_INSTRUMENTS_URL: {
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "instType": "SWAP",
                                "state": "live",
                                "settleCcy": "USDT",
                                "ctType": "linear",
                                "ctVal": "0.01",
                                "ctValCcy": "BTC",
                            },
                            {
                                "instId": "BTC-USD-SWAP",
                                "instType": "SWAP",
                                "state": "live",
                                "settleCcy": "USD",
                                "ctType": "inverse",
                            },
                        ],
                    },
                }
            )
        )

        markets = await adapter.discover_markets()

        self.assertEqual(
            [(market.symbol, market.market, market.exchange_symbol) for market in markets],
            [
                ("BTCUSDT", MarketType.SPOT, "BTC-USDT"),
                ("BTCUSDT", MarketType.PERP, "BTC-USDT-SWAP"),
            ],
        )

    async def test_parses_binance_depth_snapshot(self) -> None:
        instrument = MarketInstrument(
            exchange=Exchange.BINANCE,
            symbol="BTCUSDT",
            market=MarketType.SPOT,
            exchange_symbol="BTCUSDT",
        )
        adapter = BinanceAdapter(
            FakeHttpClient(
                {
                    "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=1000": {
                        "lastUpdateId": 42,
                        "bids": [["99.5", "2"]],
                        "asks": [["100.5", "3"]],
                    }
                }
            )
        )

        snapshot = await adapter.fetch_order_book_snapshot(instrument)

        self.assertEqual(snapshot.sequence, 42)
        self.assertEqual(str(snapshot.bids[0][0]), "99.5")
        self.assertEqual(str(snapshot.asks[0][1]), "3")


if __name__ == "__main__":
    unittest.main()
