"""Contract for the three evaluations the research loop runs on a panel.

These are the functions that turn a panel into an answer, so each one is pinned against
a case whose answer is known by construction rather than by running the code and
recording what came out. That distinction matters here more than usual: every one of
these returns a plausible-looking number when it is wrong, and the whole point of the
research loop is to be trusted when it says a signal has no edge.

The three, and the specific way each is easy to get wrong:

``rank_ic``
    Pooled and within-symbol correlation can have opposite signs. On the 2026-09-11
    sample the pooled IC was -0.049 while the within-symbol IC was +0.017: pooling lets
    the cross-sectional spread in volatility stand in for the relationship being
    measured. ``test_pooled_and_within_symbol_can_disagree_in_sign`` builds that
    situation deliberately, because a within_symbol flag that silently did the pooled
    thing would pass every other test here.

``response_curve``
    Bucketing has to partition the observations. A curve whose buckets quietly drop or
    double-count observations still plots a smooth line.

``barrier_backtest``
    Three optimistic biases were found in the live engine, all pointing the same way.
    The one reproducible on a price grid is the tie-break: when both barriers are
    satisfied the stop must win. The other two -- intrabar touches invisible at this
    sampling rate, and zero-latency fills -- are properties of the data, not of this
    code, and are recorded in the module docstring of ``research/evaluate.py`` rather
    than papered over here.

Every statistic must come back as a ``BlockStat`` so that ``n_blocks`` travels with it.
A mean without its independent sample count is the shape most of this project's earlier
wrong conclusions took.
"""

from __future__ import annotations

import unittest

import numpy as np
from research.evaluate import (
    BacktestConfig,
    barrier_backtest,
    rank_ic,
    response_curve,
)

from research.forward import BlockStat
from research.panel import Panel

STEP = 10


def make_panel(
    *,
    prices: np.ndarray,
    features: dict[str, np.ndarray] | None = None,
    symbols: tuple[str, ...] | None = None,
    tradable: np.ndarray | None = None,
) -> Panel:
    """Build a panel directly from arrays, bypassing the database.

    ``prices`` is (T, S). Everything else defaults to "present and tradable" so each
    test only has to state the part it is actually about.
    """
    prices = np.asarray(prices, dtype=np.float32)
    steps, count = prices.shape
    symbols = symbols or tuple(f"S{index}" for index in range(count))
    values = {"price": prices}
    values.update(features or {})
    values = {name: np.asarray(array, dtype=np.float32) for name, array in values.items()}
    return Panel(
        grid=np.arange(steps, dtype=np.int64) * STEP,
        symbols=symbols,
        features=values,
        categoricals={},
        tradable=np.ones_like(prices, dtype=bool) if tradable is None else tradable,
        step_seconds=STEP,
    )


def walk(start: float, returns: list[float]) -> list[float]:
    """A price path from successive simple returns, so the arithmetic stays checkable."""
    path = [start]
    for step in returns:
        path.append(path[-1] * (1 + step))
    return path


class RankICTests(unittest.TestCase):
    def test_a_feature_that_ranks_volatility_perfectly_scores_one(self) -> None:
        steps = 60
        # |return| rises monotonically with the feature, alternating sign so that the
        # relationship is with magnitude and not with direction.
        magnitudes = np.linspace(0.001, 0.02, steps)
        signs = np.where(np.arange(steps) % 2 == 0, 1.0, -1.0)
        prices = np.ones((steps + 1, 1), dtype=np.float32)
        for index in range(steps):
            prices[index + 1, 0] = prices[index, 0] * (1 + magnitudes[index] * signs[index])
        feature = np.vstack([magnitudes.reshape(-1, 1), [[np.nan]]])

        panel = make_panel(prices=prices, features={"f": feature})
        result = rank_ic(panel, "f", horizon_seconds=STEP)
        self.assertAlmostEqual(result.mean_ic, 1.0, places=6)
        self.assertEqual(result.positive_symbols, 1)
        self.assertEqual(result.n_symbols, 1)

    def test_a_reversed_feature_scores_minus_one(self) -> None:
        steps = 60
        magnitudes = np.linspace(0.001, 0.02, steps)
        signs = np.where(np.arange(steps) % 2 == 0, 1.0, -1.0)
        prices = np.ones((steps + 1, 1), dtype=np.float32)
        for index in range(steps):
            prices[index + 1, 0] = prices[index, 0] * (1 + magnitudes[index] * signs[index])
        feature = np.vstack([magnitudes[::-1].reshape(-1, 1), [[np.nan]]])

        panel = make_panel(prices=prices, features={"f": feature})
        result = rank_ic(panel, "f", horizon_seconds=STEP)
        self.assertAlmostEqual(result.mean_ic, -1.0, places=6)
        self.assertEqual(result.positive_symbols, 0)

    def test_pooled_and_within_symbol_can_disagree_in_sign(self) -> None:
        """The failure that made the original analysis report the opposite conclusion.

        Symbol QUIET is intrinsically volatile but carries small feature values; symbol
        LOUD is calm but carries large ones. Inside each symbol the feature ranks
        volatility correctly. Pooled, the between-symbol contrast dominates and flips
        the sign.
        """
        steps = 40
        rows = steps + 1
        prices = np.ones((rows, 2), dtype=np.float32)
        feature = np.full((rows, 2), np.nan, dtype=np.float32)

        quiet_magnitudes = np.linspace(0.010, 0.030, steps)  # large moves
        loud_magnitudes = np.linspace(0.0001, 0.0003, steps)  # tiny moves
        for index in range(steps):
            sign = 1.0 if index % 2 == 0 else -1.0
            prices[index + 1, 0] = prices[index, 0] * (1 + quiet_magnitudes[index] * sign)
            prices[index + 1, 1] = prices[index, 1] * (1 + loud_magnitudes[index] * sign)
        # Feature scale is inverted relative to realised volatility across symbols,
        # while remaining correctly ordered inside each one.
        feature[:steps, 0] = np.linspace(0.0, 1.0, steps)
        feature[:steps, 1] = np.linspace(100.0, 200.0, steps)

        panel = make_panel(
            prices=prices, features={"f": feature}, symbols=("QUIETUSDT", "LOUDUSDT")
        )
        within = rank_ic(panel, "f", horizon_seconds=STEP, within_symbol=True)
        pooled = rank_ic(panel, "f", horizon_seconds=STEP, within_symbol=False)

        self.assertGreater(within.mean_ic, 0.9, "inside each symbol the ranking is right")
        self.assertLess(pooled.mean_ic, 0.0, "pooled, the cross-symbol contrast dominates")

    def test_untradable_rows_are_excluded(self) -> None:
        steps = 40
        prices = np.cumprod(np.vstack([[1.0], 1 + np.full((steps, 1), 0.001)]), axis=0).astype(
            np.float32
        )
        feature = np.arange(steps + 1, dtype=np.float32).reshape(-1, 1)
        tradable = np.ones((steps + 1, 1), dtype=bool)
        tradable[: steps // 2] = False
        panel = make_panel(prices=prices, features={"f": feature}, tradable=tradable)
        result = rank_ic(panel, "f", horizon_seconds=STEP)
        self.assertLessEqual(result.n_observations, steps // 2 + 1)

    def test_a_symbol_without_enough_observations_is_not_counted(self) -> None:
        prices = np.array([[100.0], [101.0], [np.nan], [np.nan]], dtype=np.float32)
        feature = np.array([[1.0], [2.0], [np.nan], [np.nan]], dtype=np.float32)
        panel = make_panel(prices=prices, features={"f": feature})
        result = rank_ic(panel, "f", horizon_seconds=STEP)
        self.assertEqual(result.n_symbols, 0)
        self.assertTrue(np.isnan(result.mean_ic))


class ResponseCurveTests(unittest.TestCase):
    def _monotone_panel(self, steps: int = 120):
        rows = steps + 1
        prices = np.ones((rows, 1), dtype=np.float32)
        feature = np.full((rows, 1), np.nan, dtype=np.float32)
        # Forward return increases with the feature: bucket means must be ordered.
        for index in range(steps):
            step_return = (index - steps / 2) * 0.0002
            prices[index + 1, 0] = prices[index, 0] * (1 + step_return)
            feature[index, 0] = float(index)
        return make_panel(prices=prices, features={"f": feature})

    def test_buckets_partition_the_observations(self) -> None:
        panel = self._monotone_panel()
        curve = response_curve(panel, "f", horizon_seconds=STEP, buckets=5)
        self.assertEqual(len(curve.buckets), 5)
        total = sum(bucket.stat.n_observations for bucket in curve.buckets)
        finite = np.isfinite(panel.feature("f")) & np.isfinite(
            np.roll(panel.feature("price"), -1, axis=0)
        )
        self.assertEqual(total, int(finite[:-1].sum()))

    def test_bucket_edges_ascend_and_do_not_overlap(self) -> None:
        panel = self._monotone_panel()
        curve = response_curve(panel, "f", horizon_seconds=STEP, buckets=4)
        for earlier, later in zip(curve.buckets, curve.buckets[1:], strict=False):
            self.assertLessEqual(earlier.upper, later.lower)
            self.assertLess(earlier.lower, earlier.upper)

    def test_a_monotone_relationship_produces_ordered_bucket_means(self) -> None:
        panel = self._monotone_panel()
        curve = response_curve(panel, "f", horizon_seconds=STEP, buckets=4, demean=False)
        means = [bucket.stat.mean for bucket in curve.buckets]
        self.assertEqual(means, sorted(means), f"expected ordered bucket means, got {means}")

    def test_every_bucket_reports_its_independent_block_count(self) -> None:
        panel = self._monotone_panel()
        curve = response_curve(panel, "f", horizon_seconds=STEP, buckets=4)
        for bucket in curve.buckets:
            self.assertIsInstance(bucket.stat, BlockStat)
            self.assertGreaterEqual(bucket.stat.n_blocks, 1)


class BarrierBacktestTests(unittest.TestCase):
    """Entry, exit, direction and cost, each with an answer known in advance."""

    def _panel_from_path(self, path: list[float], *, score: float = 90.0, bias: float = 1.0):
        rows = len(path)
        prices = np.array(path, dtype=np.float32).reshape(-1, 1)
        scores = np.full((rows, 1), 0.0, dtype=np.float32)
        biases = np.full((rows, 1), 0.0, dtype=np.float32)
        scores[0, 0] = score
        biases[0, 0] = bias
        return make_panel(prices=prices, features={"activity_score": scores, "bias": biases})

    def _config(self, **overrides) -> BacktestConfig:
        base = {
            "entry_score": 80.0,
            "take_profit_pct": 1.0,
            "stop_loss_pct": 1.0,
            "max_holding_seconds": 10 * STEP,
            "cost_bps": 0.0,
            "max_open_positions": 10,
        }
        base.update(overrides)
        return BacktestConfig(**base)

    def test_a_long_that_reaches_the_target_exits_there(self) -> None:
        path = walk(100.0, [0.002, 0.003, 0.006])  # crosses +1% on the third step
        result = barrier_backtest(self._panel_from_path(path), self._config())
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "TAKE_PROFIT")
        self.assertEqual(trade.side, "LONG")
        self.assertGreaterEqual(trade.gross_bps, 100.0)

    def test_a_long_that_reaches_the_stop_exits_there(self) -> None:
        path = walk(100.0, [-0.002, -0.003, -0.008])
        result = barrier_backtest(self._panel_from_path(path), self._config())
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "STOP_LOSS")
        self.assertLessEqual(trade.gross_bps, -100.0)

    def test_a_path_that_touches_neither_barrier_times_out(self) -> None:
        path = walk(100.0, [0.0001] * 12)
        result = barrier_backtest(
            self._panel_from_path(path), self._config(max_holding_seconds=5 * STEP)
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "TIME_STOP")
        self.assertEqual(trade.exit_index - trade.entry_index, 5)

    def test_the_stop_wins_when_both_barriers_are_satisfied(self) -> None:
        """The optimistic tie-break found in the live engine, pinned the other way.

        With both barriers at zero, a step whose price is exactly the entry price
        satisfies ``price <= stop`` and ``price >= target`` at the same time. Which one
        is reported is entirely the tie-break, and the conservative answer is the stop.
        """
        path = walk(100.0, [0.0, 0.005])
        result = barrier_backtest(
            self._panel_from_path(path),
            self._config(take_profit_pct=0.0, stop_loss_pct=0.0),
        )
        self.assertEqual(result.trades[0].exit_reason, "STOP_LOSS")

    def test_a_short_profits_when_the_price_falls(self) -> None:
        path = walk(100.0, [-0.004, -0.009])
        result = barrier_backtest(self._panel_from_path(path, bias=-1.0), self._config())
        trade = result.trades[0]
        self.assertEqual(trade.side, "SHORT")
        self.assertEqual(trade.exit_reason, "TAKE_PROFIT")
        self.assertGreater(trade.gross_bps, 0.0, "a short gains as the price falls")

    def test_cost_is_subtracted_once_per_round_trip(self) -> None:
        path = walk(100.0, [0.002, 0.003, 0.006])
        free = barrier_backtest(self._panel_from_path(path), self._config(cost_bps=0.0))
        charged = barrier_backtest(self._panel_from_path(path), self._config(cost_bps=11.4))
        self.assertAlmostEqual(free.trades[0].gross_bps, charged.trades[0].gross_bps, places=4)
        self.assertAlmostEqual(
            charged.trades[0].net_bps, charged.trades[0].gross_bps - 11.4, places=4
        )

    def test_a_signal_below_the_entry_score_does_not_trade(self) -> None:
        path = walk(100.0, [0.002, 0.003, 0.006])
        result = barrier_backtest(self._panel_from_path(path, score=10.0), self._config())
        self.assertEqual(result.trades, ())

    def test_a_signal_with_no_bias_does_not_trade(self) -> None:
        path = walk(100.0, [0.002, 0.003, 0.006])
        result = barrier_backtest(self._panel_from_path(path, bias=0.0), self._config())
        self.assertEqual(result.trades, ())

    def test_an_untradable_row_does_not_open_a_position(self) -> None:
        path = walk(100.0, [0.002, 0.003, 0.006])
        panel = self._panel_from_path(path)
        blocked = Panel(
            grid=panel.grid,
            symbols=panel.symbols,
            features=panel.features,
            categoricals=panel.categoricals,
            tradable=np.zeros_like(panel.tradable),
            step_seconds=panel.step_seconds,
        )
        self.assertEqual(barrier_backtest(blocked, self._config()).trades, ())

    def test_open_positions_are_capped_across_symbols(self) -> None:
        """The live cap is global, not per symbol; a per-symbol reading trades more."""
        steps, count = 12, 4
        prices = np.tile(np.linspace(100.0, 100.2, steps).reshape(-1, 1), (1, count))
        scores = np.zeros((steps, count), dtype=np.float32)
        biases = np.zeros((steps, count), dtype=np.float32)
        scores[0, :] = 90.0
        biases[0, :] = 1.0
        panel = make_panel(
            prices=prices.astype(np.float32),
            features={"activity_score": scores, "bias": biases},
        )
        result = barrier_backtest(panel, self._config(max_open_positions=2))
        self.assertEqual(len(result.trades), 2, "four simultaneous signals, a cap of two")

    def test_the_control_group_matches_the_signal_group_in_shape(self) -> None:
        """Every real entry gets a same-symbol, same-side entry at a random time.

        Without this comparison a positive result cannot be distinguished from the
        barrier geometry paying off on any entry at all.
        """
        steps, count = 200, 3
        rng = np.random.default_rng(11)
        returns = rng.normal(0.0, 0.001, size=(steps, count))
        prices = 100.0 * np.cumprod(1 + returns, axis=0)
        scores = np.zeros((steps, count), dtype=np.float32)
        biases = np.zeros((steps, count), dtype=np.float32)
        scores[::40, :] = 90.0
        biases[::40, :] = 1.0
        panel = make_panel(
            prices=prices.astype(np.float32),
            features={"activity_score": scores, "bias": biases},
        )
        result = barrier_backtest(panel, self._config(max_open_positions=99))
        self.assertGreater(len(result.trades), 4)
        self.assertEqual(len(result.control), len(result.trades))
        self.assertEqual(
            sorted((t.symbol, t.side) for t in result.control),
            sorted((t.symbol, t.side) for t in result.trades),
        )
        self.assertNotEqual(
            [t.entry_index for t in result.control],
            [t.entry_index for t in result.trades],
            "a control that reuses the signal times measures nothing",
        )

    def test_the_control_group_is_reproducible_from_its_seed(self) -> None:
        steps, count = 200, 2
        rng = np.random.default_rng(5)
        prices = 100.0 * np.cumprod(1 + rng.normal(0, 0.001, size=(steps, count)), axis=0)
        scores = np.zeros((steps, count), dtype=np.float32)
        biases = np.zeros((steps, count), dtype=np.float32)
        scores[::30, :] = 90.0
        biases[::30, :] = 1.0
        panel = make_panel(
            prices=prices.astype(np.float32),
            features={"activity_score": scores, "bias": biases},
        )
        first = barrier_backtest(panel, self._config(control_seed=7, max_open_positions=99))
        again = barrier_backtest(panel, self._config(control_seed=7, max_open_positions=99))
        other = barrier_backtest(panel, self._config(control_seed=8, max_open_positions=99))
        self.assertEqual(
            [t.entry_index for t in first.control], [t.entry_index for t in again.control]
        )
        self.assertNotEqual(
            [t.entry_index for t in first.control], [t.entry_index for t in other.control]
        )

    def test_both_groups_report_block_statistics(self) -> None:
        steps, count = 300, 3
        rng = np.random.default_rng(3)
        prices = 100.0 * np.cumprod(1 + rng.normal(0, 0.001, size=(steps, count)), axis=0)
        scores = np.zeros((steps, count), dtype=np.float32)
        biases = np.zeros((steps, count), dtype=np.float32)
        scores[::25, :] = 90.0
        biases[::25, :] = 1.0
        panel = make_panel(
            prices=prices.astype(np.float32),
            features={"activity_score": scores, "bias": biases},
        )
        result = barrier_backtest(panel, self._config(max_open_positions=99))
        for stat in (result.stat, result.control_stat):
            self.assertIsInstance(stat, BlockStat)
            self.assertGreater(stat.n_observations, 0)


if __name__ == "__main__":
    unittest.main()
