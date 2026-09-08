"""Build the monitored market set from the universe and exchange listings."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.exchanges.base import ExchangeAdapter
from app.models import Exchange, MarketInstrument, UniverseAsset
from app.symbols import normalized_symbol


@dataclass(frozen=True)
class DiscoveryResult:
    markets: list[MarketInstrument]
    failures: dict[Exchange, str]


class MarketDiscoveryService:
    """Intersects active Top-50 assets with live USDT markets.

    A failed exchange discovery does not erase healthy exchange markets.  The
    failure is returned to the caller so startup health can report degraded data
    instead of silently presenting a partial universe as complete.
    """

    def __init__(self, adapters: list[ExchangeAdapter]) -> None:
        self._adapters = adapters

    async def discover(self, universe: list[UniverseAsset]) -> DiscoveryResult:
        adapter_results = await asyncio.gather(
            *(adapter.discover_markets() for adapter in self._adapters), return_exceptions=True
        )
        enabled_symbols = {normalized_symbol(asset.symbol) for asset in universe}
        markets: list[MarketInstrument] = []
        failures: dict[Exchange, str] = {}

        for adapter, result in zip(self._adapters, adapter_results, strict=True):
            if isinstance(result, BaseException):
                failures[adapter.exchange] = str(result) or type(result).__name__
                continue
            markets.extend(market for market in result if market.symbol in enabled_symbols)

        rank_by_symbol = {normalized_symbol(asset.symbol): asset.rank for asset in universe}
        unique_markets = {
            (market.exchange, market.symbol, market.market): market for market in markets
        }.values()
        ordered_markets = sorted(
            unique_markets,
            key=lambda market: (rank_by_symbol[market.symbol], market.exchange, market.market),
        )
        return DiscoveryResult(markets=ordered_markets, failures=failures)
