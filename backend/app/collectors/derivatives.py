"""Thirty-second resilient polling for perpetual derivative snapshots."""

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
        if self._initial_delay > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._initial_delay)
            except TimeoutError:
                pass
        delay = 5.0
        while not stop.is_set():
            try:
                await self._on_snapshot(await self._fetch())
                delay = 5.0
                timeout = self._interval
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, ValueError) as error:
                logger.warning("derivative_collector_retry: %s", error)
                timeout = delay
                delay = min(delay * 2, 300.0)
                await asyncio.wait_for(stop.wait(), timeout=timeout)
            except TimeoutError:
                pass
