"""Rolling aggressive trade flow, CVD, and depth-normalized pressure."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
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


_BUCKET_MS = 1_000


class _TimeBucket:
    """Sorted trades and prefix sums for one fixed-width time bucket."""

    def __init__(self) -> None:
        self.timestamps: list[int] = []
        self._entries: list[tuple[float, float]] = []
        self._buy_prefix: list[float] = [0.0]
        self._sell_prefix: list[float] = [0.0]

    def add(self, timestamp_ms: int, side: str, quote_value: float) -> None:
        index = bisect_right(self.timestamps, timestamp_ms)
        self.timestamps.insert(index, timestamp_ms)
        self._entries.insert(
            index,
            (quote_value, 0.0) if side == "BUY" else (0.0, quote_value),
        )
        if index == len(self._entries) - 1:
            self._buy_prefix.append(self._buy_prefix[-1] + self._entries[index][0])
            self._sell_prefix.append(self._sell_prefix[-1] + self._entries[index][1])
            return

        self._buy_prefix.insert(index + 1, 0.0)
        self._sell_prefix.insert(index + 1, 0.0)
        for position in range(index + 1, len(self._entries) + 1):
            buy, sell = self._entries[position - 1]
            self._buy_prefix[position] = self._buy_prefix[position - 1] + buy
            self._sell_prefix[position] = self._sell_prefix[position - 1] + sell

    def discard_before(self, cutoff_ms: int) -> bool:
        index = bisect_left(self.timestamps, cutoff_ms)
        if index == 0:
            return bool(self.timestamps)
        del self.timestamps[:index]
        del self._entries[:index]
        del self._buy_prefix[:index]
        del self._sell_prefix[:index]
        return bool(self.timestamps)

    def sums_from(self, cutoff_ms: int) -> tuple[float, float]:
        index = bisect_left(self.timestamps, cutoff_ms)
        return (
            self._buy_prefix[-1] - self._buy_prefix[index],
            self._sell_prefix[-1] - self._sell_prefix[index],
        )

    @property
    def totals(self) -> tuple[float, float]:
        return (
            self._buy_prefix[-1] - self._buy_prefix[0],
            self._sell_prefix[-1] - self._sell_prefix[0],
        )


class RollingTradeFlow:
    """Maintains a bounded one-hour trade history for one exchange market."""

    def __init__(self) -> None:
        self._buckets: dict[int, _TimeBucket] = {}
        self._bucket_indices: deque[int] = deque()
        self._latest_timestamp: int | None = None

    def add_trade(self, trade: NormalizedTrade) -> None:
        bucket_index = trade.timestamp // _BUCKET_MS
        bucket = self._buckets.get(bucket_index)
        if bucket is None:
            bucket = _TimeBucket()
            self._buckets[bucket_index] = bucket
            if not self._bucket_indices or bucket_index > self._bucket_indices[-1]:
                self._bucket_indices.append(bucket_index)
            else:
                insertion_point = bisect_right(self._bucket_indices, bucket_index)
                self._bucket_indices.insert(insertion_point, bucket_index)
        bucket.add(trade.timestamp, trade.side, trade.quote_value)

        if self._latest_timestamp is None or trade.timestamp > self._latest_timestamp:
            self._latest_timestamp = trade.timestamp
        self.prune(self._latest_timestamp)

    def prune(self, timestamp_ms: int) -> None:
        cutoff = timestamp_ms - 3_600_000
        cutoff_bucket = cutoff // _BUCKET_MS
        while self._bucket_indices and self._bucket_indices[0] < cutoff_bucket:
            del self._buckets[self._bucket_indices.popleft()]

        bucket = self._buckets.get(cutoff_bucket)
        if bucket is not None and not bucket.discard_before(cutoff):
            del self._buckets[cutoff_bucket]
            self._bucket_indices.remove(cutoff_bucket)

        if not self._bucket_indices:
            self._latest_timestamp = None

    def windows(
        self, timestamp_ms: int, requested_seconds: tuple[int, ...] | None = None
    ) -> dict[int, FlowWindow]:
        self.prune(timestamp_ms)
        seconds = WINDOWS_SECONDS if requested_seconds is None else requested_seconds
        if any(window not in WINDOWS_SECONDS for window in seconds):
            raise ValueError("requested flow window is unsupported")

        result: dict[int, FlowWindow] = {}
        for window in seconds:
            cutoff = timestamp_ms - window * 1_000
            cutoff_bucket = cutoff // _BUCKET_MS
            buy_volume = 0.0
            sell_volume = 0.0
            for bucket_index in self._bucket_indices:
                if bucket_index < cutoff_bucket:
                    continue
                bucket = self._buckets[bucket_index]
                if bucket_index == cutoff_bucket:
                    bucket_buy, bucket_sell = bucket.sums_from(cutoff)
                else:
                    bucket_buy, bucket_sell = bucket.totals
                buy_volume += bucket_buy
                sell_volume += bucket_sell

            delta = buy_volume - sell_volume
            result[window] = FlowWindow(
                buy_volume=buy_volume,
                sell_volume=sell_volume,
                delta=delta,
                buy_sell_ratio=None if sell_volume == 0 else buy_volume / sell_volume,
                cvd=delta,
            )
        return result


def pressure(aggressive_volume: float, opposing_depth: float) -> float | None:
    """Return flow pressure in x; zero or missing depth is deliberately unknown."""
    if opposing_depth <= 0:
        return None
    return aggressive_volume / opposing_depth
