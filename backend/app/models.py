"""Exchange-neutral models used by collectors and metric engines."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Exchange(StrEnum):
    BINANCE = "binance"
    OKX = "okx"


class MarketType(StrEnum):
    SPOT = "spot"
    PERP = "perp"


class UniverseAsset(BaseModel):
    """A manually curated asset in the market-cap universe."""

    model_config = ConfigDict(frozen=True)

    rank: int = Field(ge=1, le=50)
    symbol: str = Field(pattern=r"^[A-Z0-9]+$")
    name: str = Field(min_length=1)
    enabled: bool = True

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("symbol must be a string")
        return value.upper().strip()


class MarketInstrument(BaseModel):
    """A listed USDT market represented independently of exchange syntax."""

    model_config = ConfigDict(frozen=True)

    exchange: Exchange
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    market: MarketType
    exchange_symbol: str = Field(min_length=1)
    base_quantity_multiplier: float = Field(default=1.0, gt=0)


class PriceLevel(BaseModel):
    price: float = Field(gt=0)
    quantity: float = Field(gt=0)


class NormalizedTrade(BaseModel):
    exchange: Exchange
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    market: MarketType
    timestamp: int = Field(ge=0)
    price: float = Field(gt=0)
    quantity: float = Field(gt=0)
    quote_value: float = Field(gt=0)
    side: str = Field(pattern=r"^(BUY|SELL)$")


class OrderBook(BaseModel):
    exchange: Exchange
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    market: MarketType
    # Source observation time; retained for persistence and deduplication.
    timestamp: int = Field(ge=0)
    # Local receive time is independent of the exchange event time for freshness checks.
    received_at: int | None = Field(default=None, ge=0)
    available: bool = True
    bids: list[PriceLevel]
    asks: list[PriceLevel]


class DerivativeSnapshot(BaseModel):
    exchange: Exchange
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    # Source observation time; retained for persistence and OI windows.
    timestamp: int = Field(ge=0)
    received_at: int | None = Field(default=None, ge=0)
    open_interest: float = Field(ge=0)
    open_interest_usd: float = Field(ge=0)
    funding_rate: float | None
    mark_price: float = Field(gt=0)
