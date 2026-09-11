"""Public open-interest and funding snapshots for perpetual markets."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from time import monotonic, time_ns

import aiohttp

from app.http import JsonHttpClient
from app.models import DerivativeSnapshot, Exchange, MarketInstrument, MarketType

logger = logging.getLogger(__name__)


class BinanceDerivativeScheduler:
    """One paced, bounded gate shared by every Binance perpetual poller."""

    def __init__(self, symbol_count: int, *, cadence_seconds: float = 12.0) -> None:
        self._spacing = cadence_seconds / max(symbol_count, 1)
        self._semaphore = asyncio.Semaphore(3)
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._blocked_until = 0.0
        self._backoff = 2.0

    @asynccontextmanager
    async def slot(self):
        async with self._semaphore:
            await self._wait_for_turn()
            try:
                yield
            except aiohttp.ClientResponseError as error:
                await self.report_error(error)
                raise
            else:
                await self.report_success()

    async def _wait_for_turn(self) -> None:
        while True:
            async with self._lock:
                now = monotonic()
                ready_at = max(self._next_request_at, self._blocked_until)
                if ready_at <= now:
                    self._next_request_at = now + self._spacing
                    return
                delay = ready_at - now
            await asyncio.sleep(delay)

    async def report_success(self) -> None:
        async with self._lock:
            if monotonic() >= self._blocked_until:
                self._backoff = 2.0

    async def report_error(self, error: aiohttp.ClientResponseError) -> None:
        if error.status not in (418, 429):
            return
        retry_after = _retry_after_seconds(error)
        async with self._lock:
            delay = (
                max(retry_after, 60.0) if error.status == 418 else max(retry_after, self._backoff)
            )
            self._blocked_until = max(self._blocked_until, monotonic() + delay)
            if error.status == 429:
                self._backoff = min(self._backoff * 2, 60.0)
            logger.warning(
                "binance_derivative_%s: cooldown=%.1fs",
                error.status,
                delay,
            )

    @property
    def retry_delay(self) -> float:
        return max(0.0, self._blocked_until - monotonic())


class OkxDerivativeScheduler:
    """Serialize and pace complete OKX derivative snapshot request groups."""

    def __init__(self, symbol_count: int, *, cadence_seconds: float = 30.0) -> None:
        self._spacing = cadence_seconds / max(symbol_count, 1)
        self._semaphore = asyncio.Semaphore(1)
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0

    @asynccontextmanager
    async def slot(self):
        async with self._semaphore:
            while True:
                async with self._lock:
                    now = monotonic()
                    if self._next_request_at <= now:
                        self._next_request_at = now + self._spacing
                        break
                    delay = self._next_request_at - now
                await asyncio.sleep(delay)
            yield


class BinanceDerivativesProvider:
    def __init__(
        self, http_client: JsonHttpClient, scheduler: BinanceDerivativeScheduler | None = None
    ) -> None:
        self._http = http_client
        self._scheduler = scheduler or BinanceDerivativeScheduler(1)

    async def snapshot(self, instrument: MarketInstrument) -> DerivativeSnapshot:
        _require_perpetual(instrument, Exchange.BINANCE)
        async with self._scheduler.slot():
            open_interest, premium = await asyncio.gather(
                self._http.get_json(
                    f"https://fapi.binance.com/fapi/v1/openInterest?symbol={instrument.exchange_symbol}"
                ),
                self._http.get_json(
                    f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={instrument.exchange_symbol}"
                ),
            )
        received_at = time_ns() // 1_000_000
        if not isinstance(open_interest, dict) or not isinstance(premium, dict):
            raise ValueError("Binance derivative response is invalid")
        try:
            mark_price = _number(premium["markPrice"])
            oi = _number(open_interest["openInterest"]) * instrument.base_quantity_multiplier
            funding = _number(premium["lastFundingRate"])
            source_timestamp = _integer(premium.get("time", open_interest.get("time", received_at)))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Binance derivative response has invalid values") from error
        return DerivativeSnapshot(
            exchange=Exchange.BINANCE,
            symbol=instrument.symbol,
            timestamp=source_timestamp,
            received_at=received_at,
            open_interest=oi,
            open_interest_usd=oi * mark_price,
            funding_rate=funding,
            mark_price=mark_price,
        )


class OkxDerivativesProvider:
    def __init__(
        self, http_client: JsonHttpClient, scheduler: OkxDerivativeScheduler | None = None
    ) -> None:
        self._http = http_client
        self._scheduler = scheduler or OkxDerivativeScheduler(1)

    async def snapshot(self, instrument: MarketInstrument) -> DerivativeSnapshot:
        _require_perpetual(instrument, Exchange.OKX)
        async with self._scheduler.slot():
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
            oi = _number(oi_record["oi"])
            oi_usd = _number(oi_record["oiUsd"])
            rate = _number(funding_record["fundingRate"])
            mark = _number(mark_record["markPx"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("OKX derivative response has invalid values") from error
        received_at = time_ns() // 1_000_000
        try:
            source_timestamp = _integer(mark_record.get("ts", oi_record.get("ts", received_at)))
        except (TypeError, ValueError) as error:
            raise ValueError("OKX derivative response has invalid timestamp") from error
        return DerivativeSnapshot(
            exchange=Exchange.OKX,
            symbol=instrument.symbol,
            timestamp=source_timestamp,
            received_at=received_at,
            open_interest=oi,
            open_interest_usd=oi_usd,
            funding_rate=rate,
            mark_price=mark,
        )


def _retry_after_seconds(error: aiohttp.ClientResponseError) -> float:
    raw = error.headers.get("Retry-After") if error.headers else None
    try:
        return max(0.0, float(raw)) if raw is not None else 0.0
    except ValueError:
        return 0.0


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


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError("derivative numeric value has an unsupported type")
    return float(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError("derivative timestamp has an unsupported type")
    return int(value)
