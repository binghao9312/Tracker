"""Manually maintained market-cap universe providers."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from app.models import UniverseAsset


class MarketUniverseProvider(ABC):
    """Boundary for replacing the static universe with a future market-cap feed."""

    @abstractmethod
    def load(self) -> list[UniverseAsset]:
        """Return active assets ordered by market-cap rank."""


class JsonMarketUniverseProvider(MarketUniverseProvider):
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[UniverseAsset]:
        try:
            raw_assets = json.loads(self._path.read_text(encoding="utf-8"))
            assets = TypeAdapter(list[UniverseAsset]).validate_python(raw_assets)
        except (OSError, json.JSONDecodeError, ValidationError) as error:
            raise ValueError(f"invalid market universe at {self._path}") from error

        active_assets = [asset for asset in assets if asset.enabled]
        ranks = [asset.rank for asset in active_assets]
        symbols = [asset.symbol for asset in active_assets]
        if len(active_assets) != 50:
            raise ValueError("market universe must contain exactly 50 enabled assets")
        if len(set(ranks)) != len(ranks):
            raise ValueError("market universe has duplicate ranks")
        if len(set(symbols)) != len(symbols):
            raise ValueError("market universe has duplicate symbols")
        return sorted(active_assets, key=lambda asset: asset.rank)
