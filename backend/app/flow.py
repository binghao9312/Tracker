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

    def add_trade(self, trade: NormalizedTrade) -> None:
        if not self._trades or trade.timestamp >= self._trades[-1].timestamp:
            self._trades.append(trade)
        else:
            for offset, existing in enumerate(reversed(self._trades)):
                if existing.timestamp <= trade.timestamp:
                    self._trades.insert(len(self._trades) - offset, trade)
                    break
            else:
                self._trades.appendleft(trade)
        self.prune(self._trades[-1].timestamp)

    def prune(self, timestamp_ms: int) -> None:
        cutoff = timestamp_ms - 3_600_000
        while self._trades and self._trades[0].timestamp < cutoff:
            self._trades.popleft()

    def windows(
        self, timestamp_ms: int, requested_seconds: tuple[int, ...] | None = None
    ) -> dict[int, FlowWindow]:
        self.prune(timestamp_ms)
        seconds = WINDOWS_SECONDS if requested_seconds is None else requested_seconds
        if any(window not in WINDOWS_SECONDS for window in seconds):
            raise ValueError("requested flow window is unsupported")
        cutoffs = {window: timestamp_ms - window * 1_000 for window in seconds}
        buy_volumes = {window: 0.0 for window in seconds}
        sell_volumes = {window: 0.0 for window in seconds}
        oldest_cutoff = min(cutoffs.values(), default=timestamp_ms)
        for trade in reversed(self._trades):
            if trade.timestamp < oldest_cutoff:
                break
            volumes = buy_volumes if trade.side == "BUY" else sell_volumes
            for window, cutoff in cutoffs.items():
                if trade.timestamp >= cutoff:
                    volumes[window] += trade.quote_value
        return {
            window: FlowWindow(
                buy_volume=buy_volumes[window],
                sell_volume=sell_volumes[window],
                delta=buy_volumes[window] - sell_volumes[window],
                buy_sell_ratio=(
                    None
                    if sell_volumes[window] == 0
                    else buy_volumes[window] / sell_volumes[window]
                ),
                cvd=buy_volumes[window] - sell_volumes[window],
            )
            for window in seconds
        }


def pressure(aggressive_volume: float, opposing_depth: float) -> float | None:
    """Return flow pressure in x; zero or missing depth is deliberately unknown."""
    if opposing_depth <= 0:
        return None
    return aggressive_volume / opposing_depth
