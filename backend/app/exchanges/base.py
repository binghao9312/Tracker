"""Common exchange-adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import Exchange, MarketInstrument


class ExchangeAdapter(ABC):
    exchange: Exchange

    @abstractmethod
    async def discover_markets(self) -> list[MarketInstrument]:
        """Return currently tradable USDT spot and perpetual instruments."""
