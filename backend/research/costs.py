"""Pure execution-cost estimates for deciding whether a signal is tradable."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeSchedule:
    """Per-side venue fees in basis points for maker and taker execution."""

    maker_bps: float
    taker_bps: float

    def with_discount(self, fraction: float) -> FeeSchedule:
        """Return a schedule after applying a fee discount to both execution modes."""
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("discount fraction must be between 0 and 1")
        multiplier = 1.0 - fraction
        return FeeSchedule(
            maker_bps=self.maker_bps * multiplier,
            taker_bps=self.taker_bps * multiplier,
        )


def taker_round_trip_bps(fees: FeeSchedule, spread_bps: float) -> float:
    """Return the gross-alpha cost a fully taker-executed round trip must overcome."""
    return 2.0 * fees.taker_bps + spread_bps


def maker_round_trip_bps(
    fees: FeeSchedule,
    spread_bps: float,
    adverse_selection_bps: float,
) -> float:
    """Return the gross-alpha cost of a round trip filled passively on both sides."""
    return 2.0 * (fees.maker_bps + adverse_selection_bps) - spread_bps


def blended_round_trip_bps(
    fees: FeeSchedule,
    spread_bps: float,
    fill_rate: float,
    adverse_selection_bps: float,
) -> float:
    """Return expected cost when resting orders fill at the supplied round-trip rate."""
    if not 0.0 <= fill_rate <= 1.0:
        raise ValueError("fill rate must be between 0 and 1")
    maker_cost = maker_round_trip_bps(fees, spread_bps, adverse_selection_bps)
    taker_cost = taker_round_trip_bps(fees, spread_bps)
    return fill_rate * maker_cost + (1.0 - fill_rate) * taker_cost


def breakeven_alpha_bps(round_trip_cost_bps: float) -> float:
    """Return the gross-alpha threshold a candidate signal must clear to trade."""
    return round_trip_cost_bps
