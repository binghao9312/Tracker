"""Activity-score saturation scales.

Every component of the 100-point score must be able to reach its weight under
conditions that actually occur. Measured against 2.2 hours of live data, three did
not:

* ``oi_change`` saturated at 100% change in 5 minutes, while the observed p99 was
  0.76% -- so 10 of the 100 points were unreachable, contributing ~0.08.
* ``funding`` saturated at 0.1%, while the observed p99 was 0.028%.
* pressure saturated at 3.0 for both market types, but spot pressure runs ~2.5x
  smaller than perp (p99 0.94 vs 2.39), so the spot half of the 50-point pressure
  component was effectively pinned near zero and dragged the average down.

The scales are therefore configuration, not constants: recalibrating after a
volatility event must be a config change, not a code change. These tests pin the
*mechanism* -- that a component at its configured saturation earns exactly its
weight, and scales linearly below it -- rather than the specific numbers, so
retuning does not require rewriting them.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.scoring import ActivityScoreScales, activity_score, load_activity_score_scales

# Deliberately not the production defaults: these exist so the assertions below
# describe the mechanism and survive recalibration.
SCALES = ActivityScoreScales(
    spot_pressure_saturation=1.0,
    perp_pressure_saturation=3.0,
    oi_change_saturation=0.02,
    funding_saturation=0.0005,
)


class ComponentReachesItsWeightTests(unittest.TestCase):
    def test_oi_change_at_saturation_earns_its_full_ten_points(self) -> None:
        score = activity_score(oi_change=SCALES.oi_change_saturation, scales=SCALES)
        self.assertAlmostEqual(score, 10.0, places=6)

    def test_oi_change_scales_linearly_below_saturation(self) -> None:
        half = activity_score(oi_change=SCALES.oi_change_saturation / 2, scales=SCALES)
        self.assertAlmostEqual(half, 5.0, places=6)

    def test_oi_change_above_saturation_does_not_exceed_its_weight(self) -> None:
        score = activity_score(oi_change=SCALES.oi_change_saturation * 100, scales=SCALES)
        self.assertAlmostEqual(score, 10.0, places=6)

    def test_funding_at_saturation_earns_its_full_five_points(self) -> None:
        score = activity_score(funding=SCALES.funding_saturation, scales=SCALES)
        self.assertAlmostEqual(score, 5.0, places=6)

    def test_spot_pressure_saturates_on_its_own_much_smaller_scale(self) -> None:
        # 1.0 is nowhere near the old shared bar of 3.0, but it is an extreme value
        # for spot, so it must saturate the spot half of the pressure component.
        score = activity_score(
            spot_pressure_1m=SCALES.spot_pressure_saturation,
            spot_pressure_5m=SCALES.spot_pressure_saturation,
            scales=SCALES,
        )
        self.assertAlmostEqual(score, 50.0, places=6)

    def test_perp_pressure_keeps_its_own_larger_scale(self) -> None:
        at_spot_scale = activity_score(
            perp_pressure_1m=SCALES.spot_pressure_saturation,
            perp_pressure_5m=SCALES.spot_pressure_saturation,
            scales=SCALES,
        )
        at_perp_scale = activity_score(
            perp_pressure_1m=SCALES.perp_pressure_saturation,
            perp_pressure_5m=SCALES.perp_pressure_saturation,
            scales=SCALES,
        )
        # A perp reading that saturates spot must NOT saturate perp.
        self.assertLess(at_spot_scale, 50.0)
        self.assertAlmostEqual(at_perp_scale, 50.0, places=6)

    def test_both_markets_at_their_own_saturation_reach_the_full_pressure_weight(self) -> None:
        score = activity_score(
            spot_pressure_1m=SCALES.spot_pressure_saturation,
            spot_pressure_5m=SCALES.spot_pressure_saturation,
            perp_pressure_1m=SCALES.perp_pressure_saturation,
            perp_pressure_5m=SCALES.perp_pressure_saturation,
            scales=SCALES,
        )
        self.assertAlmostEqual(score, 50.0, places=6)


class PreservedSemanticsTests(unittest.TestCase):
    """Behaviour that must NOT change while the scales do."""

    def test_direction_is_still_ignored(self) -> None:
        up = activity_score(
            perp_pressure_1m=2.0, perp_pressure_5m=2.0, oi_change=0.01, scales=SCALES
        )
        down = activity_score(
            perp_pressure_1m=-2.0, perp_pressure_5m=-2.0, oi_change=-0.01, scales=SCALES
        )
        self.assertAlmostEqual(up, down, places=9)

    def test_an_absent_market_is_still_excluded_rather_than_scored_as_zero(self) -> None:
        perp_only = activity_score(
            perp_pressure_1m=SCALES.perp_pressure_saturation,
            perp_pressure_5m=SCALES.perp_pressure_saturation,
            perp_cvd_ratio=1.0,
            scales=SCALES,
        )
        self.assertAlmostEqual(perp_only, 80.0, places=6)

    def test_a_quiet_second_market_still_dilutes_the_average(self) -> None:
        # Documented, deliberate consequence of averaging available markets: a symbol
        # with a quiet spot book scores below one with no spot book at all. Changing
        # this is a separate decision, so pin it rather than leave it implicit.
        perp_only = activity_score(
            perp_pressure_1m=SCALES.perp_pressure_saturation,
            perp_pressure_5m=SCALES.perp_pressure_saturation,
            scales=SCALES,
        )
        with_quiet_spot = activity_score(
            spot_pressure_1m=0.0,
            spot_pressure_5m=0.0,
            perp_pressure_1m=SCALES.perp_pressure_saturation,
            perp_pressure_5m=SCALES.perp_pressure_saturation,
            scales=SCALES,
        )
        self.assertLess(with_quiet_spot, perp_only)

    def test_score_is_still_capped_at_one_hundred(self) -> None:
        score = activity_score(
            spot_pressure_1m=1_000.0,
            spot_pressure_5m=1_000.0,
            spot_cvd_ratio=1.0,
            perp_pressure_1m=1_000.0,
            perp_pressure_5m=1_000.0,
            perp_cvd_ratio=1.0,
            oi_change=10.0,
            funding=1.0,
            confirmed=True,
            scales=SCALES,
        )
        self.assertAlmostEqual(score, 100.0, places=6)


class RealisticConditionsTests(unittest.TestCase):
    def test_a_realistic_extreme_now_scores_far_higher_than_under_one_shared_scale(self) -> None:
        # Inputs drawn from the observed p99 of 2.2 hours of live data.
        observed = {
            "spot_pressure_1m": 0.94,
            "spot_pressure_5m": 0.94,
            "spot_cvd_ratio": 0.95,
            "perp_pressure_1m": 2.39,
            "perp_pressure_5m": 2.39,
            "perp_cvd_ratio": 0.95,
            "oi_change": 0.0076,
            "funding": 0.000277,
            "confirmed": True,
        }
        old = ActivityScoreScales(
            spot_pressure_saturation=3.0,
            perp_pressure_saturation=3.0,
            oi_change_saturation=1.0,
            funding_saturation=0.001,
        )
        self.assertGreater(
            activity_score(**observed, scales=SCALES),  # type: ignore[arg-type]
            activity_score(**observed, scales=old) + 15,  # type: ignore[arg-type]
        )


class ScaleConfigTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        directory = Path(self._directory.name)
        path = directory / "scoring.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)

    def test_a_complete_section_loads(self) -> None:
        path = self._write(
            "activity:\n"
            "  spot_pressure_saturation: 1.0\n"
            "  perp_pressure_saturation: 3.0\n"
            "  oi_change_saturation: 0.02\n"
            "  funding_saturation: 0.0005\n"
        )
        scales = load_activity_score_scales(path)
        self.assertEqual(scales.spot_pressure_saturation, 1.0)
        self.assertEqual(scales.oi_change_saturation, 0.02)

    def test_a_missing_key_is_reported_rather_than_defaulted(self) -> None:
        path = self._write(
            "activity:\n"
            "  spot_pressure_saturation: 1.0\n"
            "  perp_pressure_saturation: 3.0\n"
            "  oi_change_saturation: 0.02\n"
        )
        with self.assertRaises(ValueError):
            load_activity_score_scales(path)

    def test_an_unknown_key_is_rejected(self) -> None:
        path = self._write(
            "activity:\n"
            "  spot_pressure_saturation: 1.0\n"
            "  perp_pressure_saturation: 3.0\n"
            "  oi_change_saturation: 0.02\n"
            "  funding_saturation: 0.0005\n"
            "  pressure_saturation: 3.0\n"
        )
        with self.assertRaises(ValueError):
            load_activity_score_scales(path)

    def test_a_nonpositive_saturation_is_rejected(self) -> None:
        # A zero saturation would divide by zero and make every reading saturate.
        for bad in ("0", "-1.0"):
            path = self._write(
                "activity:\n"
                f"  spot_pressure_saturation: {bad}\n"
                "  perp_pressure_saturation: 3.0\n"
                "  oi_change_saturation: 0.02\n"
                "  funding_saturation: 0.0005\n"
            )
            with self.assertRaises(ValueError):
                load_activity_score_scales(path)

    def test_the_shipped_configuration_loads_and_is_usable(self) -> None:
        from app.config import scoring_path

        scales = load_activity_score_scales(scoring_path())
        self.assertGreater(scales.spot_pressure_saturation, 0)
        self.assertGreater(scales.perp_pressure_saturation, scales.spot_pressure_saturation)
        self.assertLess(scales.oi_change_saturation, 1.0)


if __name__ == "__main__":
    unittest.main()
