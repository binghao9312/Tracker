"""Pure execution-cost calculations for trading signals."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeSchedule:
    """Per-side maker and taker fees, expressed in basis points."""

    maker_bps: float
    taker_bps: float

    def with_discount(self, fraction: float) -> FeeSchedule:
        """Return the fee schedule after applying a fractional fee discount."""
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("discount fraction must be between 0 and 1")
        scale = 1.0 - fraction
        return FeeSchedule(
            maker_bps=self.maker_bps * scale,
            taker_bps=self.taker_bps * scale,
        )


def taker_round_trip_bps(fees: FeeSchedule, spread_bps: float) -> float:
    """Return the gross alpha a taker trade must clear to cover execution costs."""
    return 2.0 * fees.taker_bps + spread_bps


def maker_round_trip_bps(
    fees: FeeSchedule,
    spread_bps: float,
    adverse_selection_bps: float,
) -> float:
    """Return the gross alpha a filled passive trade must clear after selection effects."""
    return 2.0 * fees.maker_bps - spread_bps + 2.0 * adverse_selection_bps


def blended_round_trip_bps(
    fees: FeeSchedule,
    spread_bps: float,
    fill_rate: float,
    adverse_selection_bps: float,
) -> float:
    """Return expected execution cost when passive fills fall back to taker trades."""
    if not 0.0 <= fill_rate <= 1.0:
        raise ValueError("fill rate must be between 0 and 1")
    maker = maker_round_trip_bps(fees, spread_bps, adverse_selection_bps)
    taker = taker_round_trip_bps(fees, spread_bps)
    return fill_rate * maker + (1.0 - fill_rate) * taker


def breakeven_alpha_bps(round_trip_cost_bps: float) -> float:
    """Return the minimum gross alpha required for a signal to cover execution cost."""
    return round_trip_cost_bps
