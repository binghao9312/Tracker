"""Execution-cost model contract.

Cost is currently the binding constraint on this system, not signal quality. Measured
on 24.6h of clean data (2026-09-13/14), the live entry rule's best gross alpha is
+1.34 bps against an 11.4 bps taker round trip. Every number that decides whether a
candidate signal is worth trading comes out of this model, so the arithmetic is
pinned here rather than left to be re-derived in an ad-hoc script each time.

Verified inputs, 2026-09-14:

* Binance USDS-M futures and OKX perpetual swap both charge VIP0/Lv1
  **0.02% maker, 0.05% taker** (2 bps / 5 bps per side).
* Measured spreads, binance perp: BTC 0.013 bps, ETH ~0.04 bps, SOL 1.0 bps,
  FIL 1.2 bps. Majors sit at exactly one tick essentially always.
* Measured adverse selection on a passive fill, mid-to-mid over 5-60s:
  -0.16 to -0.38 bps, i.e. price reverts slightly. Passive execution was NOT
  adversely selected at the resolution we can observe (1.7s snapshots). This does
  not rule out sub-second adverse selection, so the model takes it as a parameter
  rather than assuming zero.

Sign convention throughout: **positive means cost**. A passive fill that earns the
spread contributes a negative number.
"""

from __future__ import annotations

import unittest

from research.costs import (
    FeeSchedule,
    blended_round_trip_bps,
    breakeven_alpha_bps,
    maker_round_trip_bps,
    taker_round_trip_bps,
)

VIP0 = FeeSchedule(maker_bps=2.0, taker_bps=5.0)


class TakerCostTests(unittest.TestCase):
    def test_taker_pays_both_fees_and_crosses_the_spread_once_per_side(self) -> None:
        # Buying at the ask costs half a spread; selling at the bid costs another half.
        # Two sides of fees plus one full spread.
        self.assertAlmostEqual(taker_round_trip_bps(VIP0, spread_bps=1.4), 11.4, places=6)

    def test_taker_cost_reproduces_the_figure_used_in_the_backtests(self) -> None:
        # The 11.4 bps that every earlier expectancy number was computed against.
        self.assertAlmostEqual(taker_round_trip_bps(VIP0, spread_bps=1.4), 11.4, places=6)

    def test_a_zero_spread_market_still_pays_both_fees(self) -> None:
        self.assertAlmostEqual(taker_round_trip_bps(VIP0, spread_bps=0.0), 10.0, places=6)


class MakerCostTests(unittest.TestCase):
    def test_a_filled_maker_earns_the_spread_instead_of_paying_it(self) -> None:
        # Resting on both sides collects a half-spread each time: a full spread credit.
        self.assertAlmostEqual(
            maker_round_trip_bps(VIP0, spread_bps=1.4, adverse_selection_bps=0.0),
            4.0 - 1.4,
            places=6,
        )

    def test_adverse_selection_is_charged_on_both_sides(self) -> None:
        got = maker_round_trip_bps(VIP0, spread_bps=1.4, adverse_selection_bps=0.5)
        self.assertAlmostEqual(got, 4.0 - 1.4 + 1.0, places=6)

    def test_favourable_selection_reduces_cost(self) -> None:
        # The measured value was negative (price reverts after the filling move).
        got = maker_round_trip_bps(VIP0, spread_bps=1.4, adverse_selection_bps=-0.3)
        self.assertAlmostEqual(got, 4.0 - 1.4 - 0.6, places=6)

    def test_maker_beats_taker_by_the_fee_gap_plus_two_half_spreads(self) -> None:
        taker = taker_round_trip_bps(VIP0, spread_bps=1.4)
        maker = maker_round_trip_bps(VIP0, spread_bps=1.4, adverse_selection_bps=0.0)
        self.assertAlmostEqual(taker - maker, 6.0 + 2.8, places=6)

    def test_enough_adverse_selection_erases_the_maker_advantage(self) -> None:
        """The break-even point a fill simulator has to be measured against."""
        taker = taker_round_trip_bps(VIP0, spread_bps=1.4)
        maker = maker_round_trip_bps(VIP0, spread_bps=1.4, adverse_selection_bps=4.4)
        self.assertAlmostEqual(maker, taker, places=6)


class BlendedCostTests(unittest.TestCase):
    """A resting order that does not fill still has to be dealt with."""

    def test_certain_fill_is_pure_maker(self) -> None:
        self.assertAlmostEqual(
            blended_round_trip_bps(VIP0, spread_bps=1.4, fill_rate=1.0, adverse_selection_bps=0.0),
            maker_round_trip_bps(VIP0, spread_bps=1.4, adverse_selection_bps=0.0),
            places=6,
        )

    def test_never_filling_falls_back_to_taker(self) -> None:
        self.assertAlmostEqual(
            blended_round_trip_bps(VIP0, spread_bps=1.4, fill_rate=0.0, adverse_selection_bps=0.0),
            taker_round_trip_bps(VIP0, spread_bps=1.4),
            places=6,
        )

    def test_partial_fill_rate_interpolates(self) -> None:
        maker = maker_round_trip_bps(VIP0, spread_bps=1.4, adverse_selection_bps=0.0)
        taker = taker_round_trip_bps(VIP0, spread_bps=1.4)
        self.assertAlmostEqual(
            blended_round_trip_bps(VIP0, spread_bps=1.4, fill_rate=0.5, adverse_selection_bps=0.0),
            0.5 * maker + 0.5 * taker,
            places=6,
        )

    def test_fill_rate_outside_zero_to_one_is_rejected(self) -> None:
        for bad in (-0.01, 1.01):
            with self.subTest(fill_rate=bad):
                with self.assertRaises(ValueError):
                    blended_round_trip_bps(
                        VIP0, spread_bps=1.4, fill_rate=bad, adverse_selection_bps=0.0
                    )


class FeeScheduleTests(unittest.TestCase):
    def test_bnb_discount_scales_both_sides(self) -> None:
        discounted = VIP0.with_discount(0.10)
        self.assertAlmostEqual(discounted.maker_bps, 1.8, places=6)
        self.assertAlmostEqual(discounted.taker_bps, 4.5, places=6)

    def test_discount_outside_zero_to_one_is_rejected(self) -> None:
        for bad in (-0.1, 1.5):
            with self.subTest(discount=bad):
                with self.assertRaises(ValueError):
                    VIP0.with_discount(bad)

    def test_negative_fees_are_allowed_because_rebates_exist(self) -> None:
        # High VIP tiers and some venues pay makers. This must not be validated away.
        rebate = FeeSchedule(maker_bps=-0.5, taker_bps=3.0)
        self.assertLess(maker_round_trip_bps(rebate, spread_bps=0.0, adverse_selection_bps=0.0), 0)


class BreakevenTests(unittest.TestCase):
    """The number Phase C signals are gated on."""

    def test_breakeven_alpha_equals_the_round_trip_cost(self) -> None:
        self.assertAlmostEqual(
            breakeven_alpha_bps(taker_round_trip_bps(VIP0, spread_bps=1.4)), 11.4, places=6
        )

    def test_maker_execution_roughly_halves_the_bar(self) -> None:
        taker_bar = breakeven_alpha_bps(taker_round_trip_bps(VIP0, spread_bps=1.4))
        maker_bar = breakeven_alpha_bps(
            maker_round_trip_bps(VIP0, spread_bps=1.4, adverse_selection_bps=-0.3)
        )
        self.assertLess(maker_bar, taker_bar / 2)

    def test_the_current_live_rule_does_not_clear_either_bar(self) -> None:
        """Measured gross alpha, 24.6h, best case across horizons and gates."""
        measured_gross_alpha_bps = 1.34
        for cost in (
            taker_round_trip_bps(VIP0, spread_bps=1.4),
            maker_round_trip_bps(VIP0, spread_bps=1.4, adverse_selection_bps=-0.3),
        ):
            self.assertLess(measured_gross_alpha_bps, breakeven_alpha_bps(cost))


if __name__ == "__main__":
    unittest.main()
