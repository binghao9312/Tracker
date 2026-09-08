"""Rolling aggressive trade flow, CVD, and depth-normalized pressure."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from app.models import NormalizedTrade

WINDOWS_SECONDS = (10, 60, 300, 900, 3600)


@dataclass(frozen=True)
class FlowWindow:
    buy_volume: float
    sell_volume: float
    delta: float
    buy_sell_ratio: float | None
    cvd: float


class RollingTradeFlow:
    """Maintains a bounded one-hour trade history for one exchange market."""

    def __init__(self) -> None:
        self._trades: deque[NormalizedTrade] = deque()
        self._cvd = 0.0

    def add_trade(self, trade: NormalizedTrade) -> None:
        self._trades.append(trade)
        self._cvd += trade.quote_value if trade.side == "BUY" else -trade.quote_value
        self.prune(trade.timestamp)

    def prune(self, timestamp_ms: int) -> None:
        cutoff = timestamp_ms - 3_600_000
        while self._trades and self._trades[0].timestamp < cutoff:
            self._trades.popleft()

    def windows(self, timestamp_ms: int) -> dict[int, FlowWindow]:
        self.prune(timestamp_ms)
        return {seconds: self._window(timestamp_ms - seconds * 1_000) for seconds in WINDOWS_SECONDS}

    def _window(self, cutoff: int) -> FlowWindow:
        buy_volume = sum(trade.quote_value for trade in self._trades if trade.timestamp >= cutoff and trade.side == "BUY")
        sell_volume = sum(trade.quote_value for trade in self._trades if trade.timestamp >= cutoff and trade.side == "SELL")
        delta = buy_volume - sell_volume
        return FlowWindow(
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            delta=delta,
            buy_sell_ratio=None if sell_volume == 0 else buy_volume / sell_volume,
            cvd=self._cvd,
        )


def pressure(aggressive_volume: float, opposing_depth: float) -> float | None:
    """Return flow pressure in x; zero or missing depth is deliberately unknown."""
    if opposing_depth <= 0:
        return None
    return aggressive_volume / opposing_depth
