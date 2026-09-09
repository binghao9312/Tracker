"""Operational commands for the backend during early implementation phases."""

from __future__ import annotations

import argparse
import asyncio

import aiohttp

from app.config import universe_path
from app.discovery import MarketDiscoveryService
from app.exchanges.binance import BinanceAdapter
from app.exchanges.okx import OkxAdapter
from app.http import AiohttpJsonClient
from app.universe import JsonMarketUniverseProvider


async def discover_live_markets() -> int:
    universe = JsonMarketUniverseProvider(universe_path()).load()
    async with aiohttp.ClientSession() as session:
        client = AiohttpJsonClient(session)
        result = await MarketDiscoveryService(
            [BinanceAdapter(client), OkxAdapter(client)]
        ).discover(universe)

    print(f"monitored_markets={len(result.markets)}")
    print(f"btc_markets={sum(market.symbol == 'BTCUSDT' for market in result.markets)}")
    for exchange, error in result.failures.items():
        print(f"{exchange}_discovery_error={error}")
    return 1 if result.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Qtrade backend operational commands")
    parser.add_argument("command", choices=["discover-live-markets"])
    args = parser.parse_args()
    if args.command == "discover-live-markets":
        return asyncio.run(discover_live_markets())
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
