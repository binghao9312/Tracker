"""Pins the forward-return estimators against known answers.

These tests exist because the failure mode of statistics code is not a crash. A
wrong standard error produces a clean, confident, wrong number, and nothing in the
output looks unusual. The only defence is fixing the expected answers for inputs
whose truth is known by construction.
"""

from __future__ import annotations

import unittest

numpy = None
try:  # research extras are optional; the rest of the suite must still run without them
    import numpy as np

    numpy = np
except ImportError:  # pragma: no cover - exercised only where extras are absent
    pass

if numpy is not None:
    from research.forward import (
        MIN_RELIABLE_BLOCKS,
        block_stats,
        demean_by_symbol,
        forward_returns,
        naive_t_stat,
    )


@unittest.skipIf(numpy is None, "requires the research extra (numpy)")
class ForwardReturnTests(unittest.TestCase):
    def test_return_is_measured_in_basis_points_over_the_horizon(self) -> None:
        prices = np.array([[100.0], [101.0], [102.0], [103.0]])

        one_step = forward_returns(prices, 1)

        self.assertAlmostEqual(one_step[0, 0], 100.0, places=6)  # +1% == 100 bps
        self.assertAlmostEqual(one_step[1, 0], 99.0099, places=3)
        self.assertTrue(np.isnan(one_step[-1, 0]))

    def test_tail_rows_have_no_future_and_stay_nan(self) -> None:
        prices = np.array([[10.0], [11.0], [12.0]])

        self.assertEqual(int(np.isnan(forward_returns(prices, 2)[:, 0]).sum()), 2)

    def test_missing_prices_propagate_rather_than_being_filled(self) -> None:
        prices = np.array([[100.0], [np.nan], [104.0]])

        result = forward_returns(prices, 1)

        self.assertTrue(np.isnan(result[0, 0]))
        self.assertTrue(np.isnan(result[1, 0]))

    def test_horizon_longer_than_the_window_yields_all_nan(self) -> None:
        prices = np.array([[100.0], [101.0]])

        self.assertTrue(np.isnan(forward_returns(prices, 5)).all())

    def test_rejects_a_non_positive_horizon(self) -> None:
        with self.assertRaises(ValueError):
            forward_returns(np.array([[1.0]]), 0)


@unittest.skipIf(numpy is None, "requires the research extra (numpy)")
class DemeanTests(unittest.TestCase):
    def test_a_constant_per_symbol_drift_is_removed_exactly(self) -> None:
        # Symbol 0 drifts +50 bps every step, symbol 1 drifts -20. Timing skill is zero
        # by construction, so a correct demean leaves nothing behind.
        returns = np.array([[50.0, -20.0], [50.0, -20.0], [50.0, -20.0]])

        self.assertTrue(np.allclose(demean_by_symbol(returns), 0.0))

    def test_symbols_are_demeaned_independently(self) -> None:
        returns = np.array([[10.0, 100.0], [30.0, 300.0]])

        result = demean_by_symbol(returns)

        self.assertTrue(np.allclose(result[:, 0], [-10.0, 10.0]))
        self.assertTrue(np.allclose(result[:, 1], [-100.0, 100.0]))

    def test_masked_rows_do_not_shift_the_baseline(self) -> None:
        # The 999 is untradable noise; it must not drag the symbol's mean.
        returns = np.array([[10.0], [30.0], [999.0]])
        mask = np.array([[True], [True], [False]])

        result = demean_by_symbol(returns, mask)

        self.assertAlmostEqual(result[0, 0], -10.0, places=6)
        self.assertAlmostEqual(result[1, 0], 10.0, places=6)

    def test_a_symbol_with_no_usable_rows_becomes_nan_not_zero(self) -> None:
        returns = np.array([[5.0, 7.0], [5.0, 9.0]])
        mask = np.array([[True, False], [True, False]])

        result = demean_by_symbol(returns, mask)

        self.assertTrue(np.allclose(result[:, 0], 0.0))
        self.assertTrue(np.isnan(result[:, 1]).all())


@unittest.skipIf(numpy is None, "requires the research extra (numpy)")
class BlockStatTests(unittest.TestCase):
    def test_blocks_are_a_fixed_partition_of_time(self) -> None:
        # Block identity depends only on the timestamp, never on which subset an
        # observation belongs to. This is what keeps subsets comparable.
        times = np.array([0, 1, 2, 10, 11, 25])

        stat = block_stats(np.ones(6), times, block_size=10, min_blocks=2)

        self.assertEqual(stat.n_blocks, len({t // 10 for t in times}))
        self.assertEqual(stat.n_blocks, 3)

    def test_a_subset_lands_on_the_same_blocks_as_the_whole(self) -> None:
        # The greedy per-subset sampler this replaced chose different timestamps for
        # each subset, which is how a combined group ended up outside the range of its
        # own halves. Under a fixed partition an observation's block never moves.
        times = np.array([0, 5, 10, 15, 20, 25])
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        subset = np.array([True, False, True, False, True, False])

        whole = block_stats(values, times, block_size=10, min_blocks=1)
        part = block_stats(values[subset], times[subset], block_size=10, min_blocks=1)

        self.assertEqual(whole.n_blocks, 3)
        self.assertEqual(part.n_blocks, 3)

    def test_cross_sectional_observations_at_one_instant_count_once(self) -> None:
        # 40 symbols at the same timestamp are one observation of the market, not 40.
        values = np.concatenate([np.full(40, 2.0), np.full(40, 4.0)])
        times = np.concatenate([np.zeros(40, int), np.full(40, 10)])

        stat = block_stats(values, times, block_size=10, min_blocks=2)

        self.assertEqual(stat.n_blocks, 2)
        self.assertEqual(stat.n_observations, 80)
        self.assertAlmostEqual(stat.mean, 3.0, places=6)

    def test_t_stat_is_withheld_when_too_few_blocks_survive(self) -> None:
        stat = block_stats(np.ones(5), np.arange(5), block_size=100)

        self.assertEqual(stat.n_blocks, 1)
        self.assertIsNone(stat.t_stat)
        self.assertFalse(stat.reliable)

    def test_reliability_needs_more_blocks_than_the_bare_minimum(self) -> None:
        rng = np.random.default_rng(0)
        n = MIN_RELIABLE_BLOCKS * 3
        times = np.arange(n)
        marginal = block_stats(rng.normal(size=20), np.arange(20), block_size=1)
        ample = block_stats(rng.normal(size=n), times, block_size=1)

        self.assertFalse(marginal.reliable)
        self.assertTrue(ample.reliable)

    def test_empty_input_is_reported_rather_than_raising(self) -> None:
        stat = block_stats(np.array([]), np.array([]), block_size=10)

        # Field by field: BlockStat equality is useless here because the mean is
        # NaN, and NaN != NaN.
        self.assertTrue(np.isnan(stat.mean))
        self.assertIsNone(stat.t_stat)
        self.assertEqual((stat.n_observations, stat.n_blocks), (0, 0))
        self.assertFalse(stat.reliable)

    def test_constant_values_give_no_t_stat_instead_of_infinity(self) -> None:
        stat = block_stats(np.full(100, 7.0), np.arange(100), block_size=1)

        self.assertIsNone(stat.t_stat)

    def test_rejects_mismatched_input_lengths(self) -> None:
        with self.assertRaises(ValueError):
            block_stats(np.ones(3), np.arange(4), block_size=1)


@unittest.skipIf(numpy is None, "requires the research extra (numpy)")
class OverlapInflationTests(unittest.TestCase):
    """The reason block_stats exists at all."""

    @staticmethod
    def _overlapping_sample(seed: int = 12345):
        """One driftless random walk, sampled at every step with a 60-step horizon.

        Truth is zero drift. Every symbol follows the same path, so the cross-section
        is perfectly correlated, and consecutive forward returns share 59 of 60 steps.
        Both dependencies that inflate a naive standard error are present.
        """
        rng = np.random.default_rng(seed)
        steps = 4_000
        symbols = 20
        path = 100 * np.exp(np.cumsum(rng.normal(0, 1e-4, steps)))
        prices = np.repeat(path[:, None], symbols, axis=1)
        horizon = 60
        returns = forward_returns(prices, horizon)
        finite = np.isfinite(returns)
        times = np.repeat(np.arange(steps)[:, None], symbols, axis=1)
        return returns[finite], times[finite], horizon

    def test_naive_standard_error_is_dramatically_overstated(self) -> None:
        values, times, horizon = self._overlapping_sample()

        blocked = block_stats(values, times, block_size=horizon)
        naive = naive_t_stat(values)

        # The naive estimator treats 80k correlated draws as independent; the blocked
        # one keeps roughly steps/horizon of them. The inflation is order-of-magnitude,
        # which is precisely how a nothing-burger gets reported as significant.
        self.assertGreater(abs(naive), abs(blocked.t_stat) * 5)

    def test_block_count_reflects_the_independent_sample_not_the_row_count(self) -> None:
        values, times, horizon = self._overlapping_sample()

        blocked = block_stats(values, times, block_size=horizon)

        self.assertGreater(blocked.n_observations, 50_000)
        self.assertLess(blocked.n_blocks, 100)

    def test_a_driftless_walk_is_not_called_significant(self) -> None:
        values, times, horizon = self._overlapping_sample()

        blocked = block_stats(values, times, block_size=horizon)

        self.assertLess(abs(blocked.t_stat), 2.5)


if __name__ == "__main__":
    unittest.main()
