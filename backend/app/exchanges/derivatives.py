"""Public open-interest and funding snapshots for perpetual markets."""

from __future__ import annotations

import asyncio
from time import time_ns

from app.http import JsonHttpClient
from app.models import DerivativeSnapshot, Exchange, MarketInstrument, MarketType


class BinanceDerivativesProvider:
    def __init__(self, http_client: JsonHttpClient) -> None:
        self._http = http_client

    async def snapshot(self, instrument: MarketInstrument) -> DerivativeSnapshot:
        _require_perpetual(instrument, Exchange.BINANCE)
        open_interest, premium = await asyncio.gather(
            self._http.get_json(
                f"https://fapi.binance.com/fapi/v1/openInterest?symbol={instrument.exchange_symbol}"
            ),
            self._http.get_json(
                f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={instrument.exchange_symbol}"
            ),
        )
        if not isinstance(open_interest, dict) or not isinstance(premium, dict):
            raise ValueError("Binance derivative response is invalid")
        try:
            mark_price = float(premium["markPrice"])
            oi = float(open_interest["openInterest"]) * instrument.base_quantity_multiplier
            funding = float(premium["lastFundingRate"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Binance derivative response has invalid values") from error
        return DerivativeSnapshot(
            exchange=Exchange.BINANCE,
            symbol=instrument.symbol,
            timestamp=time_ns() // 1_000_000,
            open_interest=oi,
            open_interest_usd=oi * mark_price,
            funding_rate=funding,
            mark_price=mark_price,
        )


class OkxDerivativesProvider:
    def __init__(self, http_client: JsonHttpClient) -> None:
        self._http = http_client
        self._semaphore = asyncio.Semaphore(1)

    async def snapshot(self, instrument: MarketInstrument) -> DerivativeSnapshot:
        _require_perpetual(instrument, Exchange.OKX)
        async with self._semaphore:
            await asyncio.sleep(0.08)
            open_interest = await self._http.get_json(
                f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={instrument.exchange_symbol}"
            )
            await asyncio.sleep(0.04)
            funding = await self._http.get_json(
                f"https://www.okx.com/api/v5/public/funding-rate?instId={instrument.exchange_symbol}"
            )
            await asyncio.sleep(0.04)
            mark_price = await self._http.get_json(
                f"https://www.okx.com/api/v5/public/mark-price?instType=SWAP&instId={instrument.exchange_symbol}"
            )
        oi_record = _okx_single_record(open_interest)
        funding_record = _okx_single_record(funding)
        mark_record = _okx_single_record(mark_price)
        try:
            oi = float(oi_record["oi"])
            oi_usd = float(oi_record["oiUsd"])
            rate = float(funding_record["fundingRate"])
            mark = float(mark_record["markPx"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("OKX derivative response has invalid values") from error
        return DerivativeSnapshot(
            exchange=Exchange.OKX,
            symbol=instrument.symbol,
            timestamp=time_ns() // 1_000_000,
            open_interest=oi,
            open_interest_usd=oi_usd,
            funding_rate=rate,
            mark_price=mark,
        )


def _require_perpetual(instrument: MarketInstrument, exchange: Exchange) -> None:
    if instrument.exchange is not exchange or instrument.market is not MarketType.PERP:
        raise ValueError(f"expected {exchange.value} perpetual instrument")


def _okx_single_record(payload: object) -> dict[str, object]:
    if (
        not isinstance(payload, dict)
        or payload.get("code") != "0"
        or not isinstance(payload.get("data"), list)
    ):
        raise ValueError("OKX derivative response is invalid")
    records = [record for record in payload["data"] if isinstance(record, dict)]
    if len(records) != 1:
        raise ValueError("OKX derivative response must have one record")
    return records[0]
