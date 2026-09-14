"""Contract for the trivial-baseline comparison that gates Phase E.

The one positive result this system has produced is that `activity_score` predicts the
*size* of the next move: within-symbol rank IC around +0.03, reproduced twice by
unrelated code. Phase E would build position sizing on it.

Before that is worth doing, it has to clear the bar that no one has yet put in front of
it. Volatility clusters. "How much did this symbol move over the last five minutes"
predicts how much it will move over the next five minutes, and it needs no order books,
no cross-exchange aggregation, and no scoring config. If that trivial baseline scores as
well, then `activity_score` has no incremental value and the correct conclusion is that
the system's one positive result is a rediscovery of volatility clustering.

So this pins three quantities rather than one:

* the feature's own rank IC against the size of the forward move,
* the baseline's rank IC against the same thing,
* and the feature's IC **after the baseline has been partialled out**, which is the one
  that actually decides Phase E. A feature can have a healthy raw IC and zero
  incremental IC, and that case looks like success in every report that omits it.

Partialling is done on ranks: regress the feature's ranks and the target's ranks on the
control's ranks, then correlate the residuals. That is Spearman partial correlation, and
it is the rank-based analogue of asking what the feature knows that the control does not.
"""

from __future__ import annotations

import unittest

import numpy as np

from research.evaluate import rank_ic, trailing_volatility
from research.panel import Panel

STEP = 10


def make_panel(prices: np.ndarray, **features: np.ndarray) -> Panel:
    prices = np.asarray(prices, dtype=np.float32)
    steps, count = prices.shape
    values = {"price": prices}
    values.update({name: np.asarray(v, dtype=np.float32) for name, v in features.items()})
    return Panel(
        grid=np.arange(steps, dtype=np.int64) * STEP,
        symbols=tuple(f"S{i}" for i in range(count)),
        features=values,
        categoricals={},
        tradable=np.ones_like(prices, dtype=bool),
        step_seconds=STEP,
    )


def alternating_walk(magnitudes: np.ndarray, start: float = 100.0) -> np.ndarray:
    """A price path whose step sizes are ``magnitudes`` and whose signs alternate."""
    prices = np.empty(magnitudes.size + 1, dtype=np.float64)
    prices[0] = start
    for index, magnitude in enumerate(magnitudes):
        sign = 1.0 if index % 2 == 0 else -1.0
        prices[index + 1] = prices[index] * (1 + magnitude * sign)
    return prices


class TrailingVolatilityTests(unittest.TestCase):
    def test_it_looks_backwards_only(self) -> None:
        """A forward-looking baseline would beat anything and mean nothing."""
        steps = 60
        # Dead flat, then violent. A backward-looking measure must stay at zero right up
        # to the jump; a forward-looking one would light up before it.
        prices = np.concatenate([np.full(steps, 100.0), 100.0 * np.cumprod(np.full(steps, 1.05))])
        panel = make_panel(prices.reshape(-1, 1))
        volatility = trailing_volatility(panel, window_seconds=5 * STEP)[:, 0]
        self.assertTrue(
            np.all(np.nan_to_num(volatility[:steps]) == 0.0),
            "trailing volatility must be zero while the price is flat",
        )
        self.assertGreater(volatility[steps + 6], 0.0, "and must react once it is not")

    def test_it_ranks_a_quiet_symbol_below_a_noisy_one(self) -> None:
        steps = 80
        quiet = alternating_walk(np.full(steps, 0.0002))
        noisy = alternating_walk(np.full(steps, 0.01))
        panel = make_panel(np.column_stack([quiet, noisy]))
        volatility = trailing_volatility(panel, window_seconds=10 * STEP)
        settled = volatility[20:]
        self.assertTrue(np.all(settled[:, 1] > settled[:, 0]))

    def test_the_warmup_is_nan_rather_than_zero(self) -> None:
        """Zero would be read as a genuine observation of "perfectly calm"."""
        prices = alternating_walk(np.full(40, 0.001)).reshape(-1, 1)
        panel = make_panel(prices)
        volatility = trailing_volatility(panel, window_seconds=10 * STEP)
        self.assertTrue(np.isnan(volatility[0, 0]))
        self.assertTrue(np.isfinite(volatility[15, 0]))

    def test_a_window_shorter_than_one_step_is_rejected(self) -> None:
        panel = make_panel(alternating_walk(np.full(20, 0.001)).reshape(-1, 1))
        with self.assertRaises(ValueError):
            trailing_volatility(panel, window_seconds=STEP // 2)


class PartialRankICTests(unittest.TestCase):
    """Controlling for the baseline is what decides Phase E."""

    def _panel(self, steps: int = 120):
        magnitudes = np.abs(np.sin(np.linspace(0, 9, steps))) * 0.01 + 0.0005
        prices = alternating_walk(magnitudes)
        # A feature that sees the forward magnitude exactly.
        oracle_feature = np.full(steps + 1, np.nan)
        oracle_feature[:steps] = magnitudes
        return make_panel(prices.reshape(-1, 1), oracle=oracle_feature.reshape(-1, 1))

    def test_a_feature_controlled_against_itself_has_no_incremental_value(self) -> None:
        panel = self._panel()
        raw = rank_ic(panel, "oracle", horizon_seconds=STEP)
        controlled = rank_ic(panel, "oracle", horizon_seconds=STEP, control="oracle")
        self.assertGreater(raw.mean_ic, 0.9)
        self.assertAlmostEqual(controlled.mean_ic, 0.0, places=6)

    def test_controlling_for_an_unrelated_series_leaves_the_ic_alone(self) -> None:
        panel = self._panel()
        noise = np.random.default_rng(3).normal(size=panel.shape).astype(np.float32)
        panel.features["noise"] = noise
        raw = rank_ic(panel, "oracle", horizon_seconds=STEP)
        controlled = rank_ic(panel, "oracle", horizon_seconds=STEP, control="noise")
        self.assertAlmostEqual(controlled.mean_ic, raw.mean_ic, delta=0.15)

    def test_a_feature_that_only_restates_the_control_scores_zero(self) -> None:
        """The Phase E failure case: a healthy raw IC and nothing the baseline lacks."""
        panel = self._panel()
        # A strictly increasing transform of the control preserves every rank.
        restated = np.log1p(np.nan_to_num(panel.feature("oracle"), nan=0.0) * 5.0) + 1.0
        restated[~np.isfinite(panel.feature("oracle"))] = np.nan
        panel.features["restated"] = restated.astype(np.float32)
        raw = rank_ic(panel, "restated", horizon_seconds=STEP)
        controlled = rank_ic(panel, "restated", horizon_seconds=STEP, control="oracle")
        self.assertGreater(raw.mean_ic, 0.9, "it looks excellent on its own")
        self.assertAlmostEqual(controlled.mean_ic, 0.0, places=6, msg="and adds nothing")

    def test_an_array_can_be_passed_instead_of_a_feature_name(self) -> None:
        """The baseline is computed, not stored, so it has to be usable directly."""
        panel = self._panel()
        by_name = rank_ic(panel, "oracle", horizon_seconds=STEP)
        by_array = rank_ic(panel, panel.feature("oracle"), horizon_seconds=STEP)
        self.assertAlmostEqual(by_name.mean_ic, by_array.mean_ic, places=9)
        self.assertEqual(by_array.feature, "<array>")

    def test_controlling_still_reports_the_symbols_it_used(self) -> None:
        panel = self._panel()
        controlled = rank_ic(panel, "oracle", horizon_seconds=STEP, control="oracle")
        self.assertEqual(controlled.n_symbols, 1)
        self.assertGreater(controlled.n_observations, 0)


if __name__ == "__main__":
    unittest.main()
