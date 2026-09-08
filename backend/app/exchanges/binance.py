"""Binance public instrument discovery."""

from __future__ import annotations

import asyncio
from time import time_ns


from app.exchanges.base import ExchangeAdapter
from app.http import JsonHttpClient
from app.models import Exchange, MarketInstrument, MarketType
from app.symbols import normalized_symbol

SPOT_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
PERP_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"


class BinanceAdapter(ExchangeAdapter):
    exchange = Exchange.BINANCE

    def __init__(self, http_client: JsonHttpClient) -> None:
        self._http_client = http_client

    async def discover_markets(self) -> list[MarketInstrument]:
        spot_payload, perp_payload = await asyncio.gather(
            self._http_client.get_json(SPOT_EXCHANGE_INFO_URL),
            self._http_client.get_json(PERP_EXCHANGE_INFO_URL),
        )
        return [
            *self._parse_spot(spot_payload),
            *self._parse_perp(perp_payload),
        ]

    @staticmethod
    def _symbols(payload: object) -> list[dict[str, object]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise ValueError("Binance exchangeInfo response has no symbols array")
        return [item for item in payload["symbols"] if isinstance(item, dict)]

    def _parse_spot(self, payload: object) -> list[MarketInstrument]:
        instruments: list[MarketInstrument] = []
        for item in self._symbols(payload):
            if (
                item.get("status") != "TRADING"
                or item.get("quoteAsset") != "USDT"
                or item.get("isSpotTradingAllowed") is not True
            ):
                continue
            base_asset = item.get("baseAsset")
            exchange_symbol = item.get("symbol")
            if not isinstance(base_asset, str) or not isinstance(exchange_symbol, str):
                continue
            try:
                symbol = normalized_symbol(base_asset)
            except ValueError:
                continue
            instruments.append(
                MarketInstrument(
                    exchange=self.exchange,
                    symbol=symbol,
                    market=MarketType.SPOT,
                    exchange_symbol=exchange_symbol,
                )
            )
        return instruments

    def _parse_perp(self, payload: object) -> list[MarketInstrument]:
        instruments: list[MarketInstrument] = []
        for item in self._symbols(payload):
            if (
                item.get("status") != "TRADING"
                or item.get("quoteAsset") != "USDT"
                or item.get("contractType") != "PERPETUAL"
            ):
                continue
            base_asset = item.get("baseAsset")
            exchange_symbol = item.get("symbol")
            if not isinstance(base_asset, str) or not isinstance(exchange_symbol, str):
                continue
            try:
                symbol = normalized_symbol(base_asset)
            except ValueError:
                continue
            instruments.append(
                MarketInstrument(
                    exchange=self.exchange,
                    symbol=symbol,
                    market=MarketType.PERP,
                    exchange_symbol=exchange_symbol,
                )
            )
        return instruments

    async def fetch_order_book_snapshot(
        self, instrument: MarketInstrument
    ) -> "SequencedOrderBookSnapshot":
        """Fetch the REST baseline required before applying depth events."""
        from app.orderbook import SequencedOrderBookSnapshot

        if instrument.exchange is not self.exchange:
            raise ValueError("instrument does not belong to Binance")
        endpoint = (
            "https://api.binance.com/api/v3/depth"
            if instrument.market is MarketType.SPOT
            else "https://fapi.binance.com/fapi/v1/depth"
        )
        payload = await self._http_client.get_json(
            f"{endpoint}?symbol={instrument.exchange_symbol}&limit=1000"
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("lastUpdateId"), int):
            raise ValueError("Binance depth snapshot is invalid")
        return SequencedOrderBookSnapshot(
            exchange=self.exchange,
            symbol=instrument.symbol,
            market=instrument.market,
            sequence=payload["lastUpdateId"],
            timestamp=time_ns() // 1_000_000,
            bids=self._parse_depth_levels(payload.get("bids")),
            asks=self._parse_depth_levels(payload.get("asks")),
        )

    @staticmethod
    def _parse_depth_levels(raw_levels: object) -> list[tuple["Decimal", "Decimal"]]:
        from decimal import Decimal, InvalidOperation

        if not isinstance(raw_levels, list):
            raise ValueError("Binance depth snapshot has no price levels")
        levels: list[tuple[Decimal, Decimal]] = []
        for level in raw_levels:
            if not isinstance(level, list) or len(level) < 2:
                raise ValueError("Binance depth level is invalid")
            try:
                price, quantity = Decimal(str(level[0])), Decimal(str(level[1]))
            except InvalidOperation as error:
                raise ValueError("Binance depth level is not numeric") from error
            levels.append((price, quantity))
        return levels
