"""Small public-market-data HTTP boundary shared by exchange adapters."""

from __future__ import annotations

from typing import Protocol

import aiohttp


class JsonHttpClient(Protocol):
    async def get_json(self, url: str) -> object: ...


class AiohttpJsonClient:
    """Fetch public JSON with explicit timeout and response validation."""

    def __init__(self, session: aiohttp.ClientSession, timeout_seconds: float = 10.0) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def get_json(self, url: str) -> object:
        async with self._session.get(url, timeout=self._timeout) as response:
            response.raise_for_status()
            return await response.json(content_type=None)
