"""Perpetual open-interest change and funding-state calculations."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from app.models import DerivativeSnapshot


class FundingState(StrEnum):
    NORMAL = "normal"
    ELEVATED_POSITIVE = "elevated_positive"
    ELEVATED_NEGATIVE = "elevated_negative"


@dataclass(frozen=True)
class DerivativeMetrics:
    snapshot: DerivativeSnapshot
    oi_change_1m: float | None
    oi_change_5m: float | None
    oi_change_15m: float | None
    oi_change_1h: float | None


class OpenInterestHistory:
    """Retains only enough samples to calculate the specified one-hour deltas."""

    def __init__(self) -> None:
        self._samples: deque[DerivativeSnapshot] = deque()

    def add(self, snapshot: DerivativeSnapshot) -> None:
        if self._samples and snapshot.timestamp < self._samples[-1].timestamp:
            raise ValueError("open-interest snapshots must be chronological")
        self._samples.append(snapshot)
        cutoff = snapshot.timestamp - 3_600_000
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

    def metrics(self) -> DerivativeMetrics:
        if not self._samples:
            raise ValueError("no open-interest snapshot available")
        snapshot = self._samples[-1]
        return DerivativeMetrics(
            snapshot=snapshot,
            oi_change_1m=self._change_at(snapshot.timestamp - 60_000),
            oi_change_5m=self._change_at(snapshot.timestamp - 300_000),
            oi_change_15m=self._change_at(snapshot.timestamp - 900_000),
            oi_change_1h=self._change_at(snapshot.timestamp - 3_600_000),
        )

    def _change_at(self, cutoff: int) -> float | None:
        current = self._samples[-1].open_interest
        prior = next((item for item in reversed(self._samples) if item.timestamp <= cutoff), None)
        if prior is None or prior.open_interest == 0:
            return None
        return (current - prior.open_interest) / prior.open_interest


def funding_state(rate: float | None, positive_threshold: float, negative_threshold: float) -> FundingState:
    if rate is None or negative_threshold < rate < positive_threshold:
        return FundingState.NORMAL
    return FundingState.ELEVATED_POSITIVE if rate >= positive_threshold else FundingState.ELEVATED_NEGATIVE
