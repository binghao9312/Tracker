"""OKX public instrument discovery."""

from __future__ import annotations

import asyncio

from app.exchanges.base import ExchangeAdapter
from app.http import JsonHttpClient
from app.models import Exchange, MarketInstrument, MarketType
from app.symbols import normalize_okx_instrument

SPOT_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments?instType=SPOT"
SWAP_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"


class OkxAdapter(ExchangeAdapter):
    exchange = Exchange.OKX

    def __init__(self, http_client: JsonHttpClient) -> None:
        self._http_client = http_client

    async def discover_markets(self) -> list[MarketInstrument]:
        spot_payload, swap_payload = await asyncio.gather(
            self._http_client.get_json(SPOT_INSTRUMENTS_URL),
            self._http_client.get_json(SWAP_INSTRUMENTS_URL),
        )
        return [
            *self._parse_spot(spot_payload),
            *self._parse_swap(swap_payload),
        ]

    @staticmethod
    def _data(payload: object) -> list[dict[str, object]]:
        if (
            not isinstance(payload, dict)
            or payload.get("code") != "0"
            or not isinstance(payload.get("data"), list)
        ):
            raise ValueError("OKX instruments response is invalid")
        return [item for item in payload["data"] if isinstance(item, dict)]

    def _parse_spot(self, payload: object) -> list[MarketInstrument]:
        instruments: list[MarketInstrument] = []
        for item in self._data(payload):
            instrument_id = item.get("instId")
            if (
                item.get("instType") != "SPOT"
                or item.get("state") != "live"
                or item.get("quoteCcy") != "USDT"
                or not isinstance(instrument_id, str)
            ):
                continue
            try:
                symbol = normalize_okx_instrument(instrument_id, MarketType.SPOT)
            except ValueError:
                continue
            instruments.append(
                MarketInstrument(
                    exchange=self.exchange,
                    symbol=symbol,
                    market=MarketType.SPOT,
                    exchange_symbol=instrument_id,
                )
            )
        return instruments

    def _parse_swap(self, payload: object) -> list[MarketInstrument]:
        instruments: list[MarketInstrument] = []
        for item in self._data(payload):
            instrument_id = item.get("instId")
            if (
                item.get("instType") != "SWAP"
                or item.get("state") != "live"
                or item.get("settleCcy") != "USDT"
                or item.get("ctType") != "linear"
                or not isinstance(instrument_id, str)
            ):
                continue
            try:
                symbol = normalize_okx_instrument(instrument_id, MarketType.PERP)
                multiplier = float(item["ctVal"])
            except (KeyError, TypeError, ValueError):
                continue
            if multiplier <= 0 or item.get("ctValCcy") != instrument_id.split("-")[0]:
                continue
            instruments.append(
                MarketInstrument(
                    exchange=self.exchange,
                    symbol=symbol,
                    market=MarketType.PERP,
                    exchange_symbol=instrument_id,
                    base_quantity_multiplier=multiplier,
                )
            )
        return instruments

    async def fetch_order_book_snapshot(
        self, instrument: MarketInstrument
    ) -> "SequencedOrderBookSnapshot":
        """Fetch the REST baseline required before applying books-channel updates."""
        from app.orderbook import SequencedOrderBookSnapshot

        if instrument.exchange is not self.exchange:
            raise ValueError("instrument does not belong to OKX")
        payload = await self._http_client.get_json(
            f"https://www.okx.com/api/v5/market/books?instId={instrument.exchange_symbol}&sz=400"
        )
        records = self._data(payload)
        if len(records) != 1 or not isinstance(records[0].get("seqId"), str):
            raise ValueError("OKX depth snapshot is invalid")
        record = records[0]
        try:
            sequence = int(record["seqId"])
            timestamp = int(record["ts"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("OKX depth snapshot has invalid sequencing") from error
        return SequencedOrderBookSnapshot(
            exchange=self.exchange,
            symbol=instrument.symbol,
            market=instrument.market,
            sequence=sequence,
            timestamp=timestamp,
            bids=self._parse_depth_levels(record.get("bids")),
            asks=self._parse_depth_levels(record.get("asks")),
        )

    @staticmethod
    def _parse_depth_levels(raw_levels: object) -> list[tuple["Decimal", "Decimal"]]:
        from decimal import Decimal, InvalidOperation

        if not isinstance(raw_levels, list):
            raise ValueError("OKX depth snapshot has no price levels")
        levels: list[tuple[Decimal, Decimal]] = []
        for level in raw_levels:
            if not isinstance(level, list) or len(level) < 2:
                raise ValueError("OKX depth level is invalid")
            try:
                price, quantity = Decimal(str(level[0])), Decimal(str(level[1]))
            except InvalidOperation as error:
                raise ValueError("OKX depth level is not numeric") from error
            levels.append((price, quantity))
        return levels
