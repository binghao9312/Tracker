"""Translation between exchange instruments and internal symbols."""

from __future__ import annotations

from app.models import MarketType

USDT = "USDT"


def normalized_symbol(base_asset: str, quote_asset: str = USDT) -> str:
    """Return the canonical internal symbol for a USDT market.

    The scanner intentionally has one internal identity (for example ``BTCUSDT``)
    regardless of whether an exchange spells the instrument ``BTC-USDT`` or
    ``BTC-USDT-SWAP``.
    """
    base = base_asset.upper().strip()
    quote = quote_asset.upper().strip()
    if not base or not base.isascii() or not base.isalnum():
        raise ValueError("base asset must be a non-empty ASCII alphanumeric symbol")
    if quote != USDT:
        raise ValueError(f"only {USDT} quoted markets are supported")
    return f"{base}{USDT}"


def normalize_okx_instrument(instrument_id: str, market: MarketType) -> str:
    """Normalize an OKX SPOT or linear SWAP instrument identifier."""
    parts = instrument_id.upper().split("-")
    expected_parts = 2 if market is MarketType.SPOT else 3
    expected_suffix = None if market is MarketType.SPOT else "SWAP"
    if len(parts) != expected_parts or parts[1] != USDT or (
        expected_suffix is not None and parts[2] != expected_suffix
    ):
        raise ValueError(f"invalid OKX {market.value} USDT instrument: {instrument_id}")
    return normalized_symbol(parts[0])
