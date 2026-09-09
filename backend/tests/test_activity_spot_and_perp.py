import unittest

from app.scoring import activity_score


class ActivitySpotAndPerpRegressionTests(unittest.TestCase):
    def test_spot_only_and_perp_only_activity_are_not_diluted_by_an_absent_market(self) -> None:
        spot_only = activity_score(
            spot_pressure_1m=3.0,
            spot_pressure_5m=3.0,
            spot_cvd_ratio=1.0,
        )
        perp_only = activity_score(
            perp_pressure_1m=3.0,
            perp_pressure_5m=3.0,
            perp_cvd_ratio=1.0,
        )

        self.assertEqual(spot_only, 80)
        self.assertEqual(perp_only, 80)

    def test_buy_and_sell_pressure_and_flow_have_symmetric_magnitudes(self) -> None:
        buy = activity_score(
            spot_pressure_1m=3.0,
            spot_pressure_5m=3.0,
            spot_cvd_ratio=1.0,
            perp_pressure_1m=1.5,
            perp_pressure_5m=1.5,
            perp_cvd_ratio=0.5,
        )
        sell = activity_score(
            spot_pressure_1m=-3.0,
            spot_pressure_5m=-3.0,
            spot_cvd_ratio=-1.0,
            perp_pressure_1m=-1.5,
            perp_pressure_5m=-1.5,
            perp_cvd_ratio=-0.5,
        )

        self.assertEqual(buy, sell)


if __name__ == "__main__":
    unittest.main()
