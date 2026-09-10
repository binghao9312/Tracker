"""Resilient polling for perpetual derivative snapshots."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp

from app.models import DerivativeSnapshot

logger = logging.getLogger(__name__)
SnapshotFetcher = Callable[[], Awaitable[DerivativeSnapshot]]
SnapshotCallback = Callable[[DerivativeSnapshot], Awaitable[None]]


class DerivativePollingCollector:
    def __init__(
        self,
        fetch: SnapshotFetcher,
        on_snapshot: SnapshotCallback,
        interval_seconds: float = 30.0,
        initial_delay_seconds: float = 0.0,
    ) -> None:
        self._fetch = fetch
        self._on_snapshot = on_snapshot
        self._interval = interval_seconds
        self._initial_delay = initial_delay_seconds

    async def run(self, stop: asyncio.Event) -> None:
        if await self._sleep_or_stop(stop, self._initial_delay):
            return
        delay = 2.0
        while not stop.is_set():
            timeout = self._interval
            try:
                await self._on_snapshot(await self._fetch())
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except aiohttp.ClientResponseError as error:
                timeout = _status_retry_delay(error, delay)
                delay = min(delay * 2, 60.0)
            except (aiohttp.ClientError, OSError, ValueError) as error:
                logger.warning("derivative_collector_retry: %s", error)
                timeout = delay
                delay = min(delay * 2, 300.0)
            if await self._sleep_or_stop(stop, timeout):
                return

    @staticmethod
    async def _sleep_or_stop(stop: asyncio.Event, delay: float) -> bool:
        if delay <= 0:
            return stop.is_set()
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True


def _status_retry_delay(error: aiohttp.ClientResponseError, fallback: float) -> float:
    raw = error.headers.get("Retry-After") if error.headers else None
    try:
        retry_after = float(raw) if raw is not None else 0.0
    except ValueError:
        retry_after = 0.0
    if error.status == 418:
        return max(60.0, retry_after)
    if error.status == 429:
        return max(min(fallback, 60.0), retry_after)
    logger.warning("derivative_collector_retry: %s", error)
    return fallback
