import unittest

from app.discovery import MarketDiscoveryService
from app.exchanges.base import ExchangeAdapter
from app.models import Exchange, MarketInstrument, MarketType, UniverseAsset


class StaticAdapter(ExchangeAdapter):
    def __init__(self, exchange: Exchange, markets: list[MarketInstrument] | Exception) -> None:
        self.exchange = exchange
        self._markets = markets

    async def discover_markets(self) -> list[MarketInstrument]:
        if isinstance(self._markets, Exception):
            raise self._markets
        return self._markets


class MarketDiscoveryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_intersects_universe_deduplicates_and_keeps_partial_results(self) -> None:
        universe = [
            UniverseAsset(rank=1, symbol="BTC", name="Bitcoin"),
            UniverseAsset(rank=2, symbol="ETH", name="Ethereum"),
        ]
        btc_spot = MarketInstrument(
            exchange=Exchange.BINANCE,
            symbol="BTCUSDT",
            market=MarketType.SPOT,
            exchange_symbol="BTCUSDT",
        )
        service = MarketDiscoveryService(
            [
                StaticAdapter(Exchange.BINANCE, [btc_spot, btc_spot]),
                StaticAdapter(Exchange.OKX, RuntimeError("maintenance")),
            ]
        )

        result = await service.discover(universe)

        self.assertEqual(result.markets, [btc_spot])
        self.assertEqual(result.failures, {Exchange.OKX: "maintenance"})


if __name__ == "__main__":
    unittest.main()
