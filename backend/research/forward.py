"""Forward-return statistics for signal research.

Three primitives, each of which exists because the obvious alternative silently
produces a confident wrong answer rather than an error:

``forward_returns``
    Realised return from t to t+h, in basis points, on an evenly spaced grid.

``demean_by_symbol``
    Removes each symbol's own mean return over the window. Without this, a sample
    drawn from a trending market credits the market's drift to the signal. The
    2026-09-09..11 sample had 39 of 43 symbols down a median 6%; a short-biased
    signal measured there looks profitable purely from beta.

``block_stats``
    Mean and t-statistic computed over non-overlapping time blocks. Two distinct
    dependencies make the naive standard error far too small:

    * *Overlap.* On a 10s grid a 60m forward return shares 359 of its 360 steps
      with its neighbour, so consecutive observations are nearly the same draw.
    * *Cross-section.* Crypto symbols move together, so 46 symbols at one instant
      are closer to one observation than to 46.

    Clustering on the timestamp handles the second; thinning to non-overlapping
    blocks handles the first. ``BlockStat.n_blocks`` reports how many independent
    observations actually back the number, which is the only honest denominator.

Every estimator here is a pure function of arrays so it can be pinned by tests with
known answers. That matters more than usual: a wrong standard error runs perfectly
cleanly and is invisible in any output you would think to look at.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from math import isfinite

import numpy as np

BPS = 10_000.0

# Below this many independent blocks a t-statistic is not worth reporting: the
# sampling distribution of the estimate is too wide for the number to discriminate
# between "there is an effect" and "there is not".
MIN_RELIABLE_BLOCKS = 30


@dataclass(frozen=True)
class BlockStat:
    """A mean with the sample size that actually backs it.

    Two means, deliberately. ``mean`` averages every observation; ``block_mean``
    averages the block means, which is the quantity ``t_stat`` actually tests. They
    differ whenever blocks hold unequal numbers of observations, and they can even
    disagree in sign -- four observations of +10 in one block and one of -25 in
    another pool to +3.0 while the blocks average -7.5. Reporting the pooled mean
    beside a t-statistic computed from the other one reads as a contradiction, so
    :meth:`describe` quotes the one the test is about and says when they diverge.
    """

    mean: float
    t_stat: float | None
    n_observations: int
    n_blocks: int
    block_mean: float = float("nan")

    @property
    def reliable(self) -> bool:
        """Whether ``t_stat`` rests on enough independent blocks to be worth quoting."""
        return self.t_stat is not None and self.n_blocks >= MIN_RELIABLE_BLOCKS

    def describe(self, *, unit: str = "bps") -> str:
        if self.t_stat is None:
            return f"{self.mean:+.1f} {unit} (t n/a, k={self.n_blocks})"
        flag = "" if self.reliable else f"  [k<{MIN_RELIABLE_BLOCKS}: unreliable]"
        # Quote the block mean, because that is what the t-statistic is a test of.
        shown = self.block_mean if isfinite(self.block_mean) else self.mean
        skew = ""
        if isfinite(self.block_mean) and (self.block_mean > 0) != (self.mean > 0):
            skew = f"  [pooled {self.mean:+.1f}: unequal blocks]"
        return f"{shown:+.1f} {unit} (t={self.t_stat:+.2f}, k={self.n_blocks}){flag}{skew}"


def forward_returns(prices: np.ndarray, horizon_steps: int) -> np.ndarray:
    """Return the (T, S) forward return in bps from each row to ``horizon_steps`` later.

    ``prices`` is a (T, S) grid of mid prices with NaN where no price was available.
    The last ``horizon_steps`` rows are NaN because their future is outside the window.
    """
    if horizon_steps < 1:
        raise ValueError("horizon_steps must be at least 1")
    prices = np.asarray(prices, dtype=float)
    if prices.ndim != 2:
        raise ValueError("prices must be a (T, S) array")
    out = np.full_like(prices, np.nan)
    if horizon_steps < len(prices):
        with np.errstate(invalid="ignore", divide="ignore"):
            out[:-horizon_steps] = prices[horizon_steps:] / prices[:-horizon_steps] - 1.0
    return out * BPS


def demean_by_symbol(returns: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    """Subtract each symbol's own mean forward return over the window.

    What survives is timing: whether the signal fired at better-than-average moments
    for that symbol. What is removed is everything that would have accrued to holding
    the symbol regardless of when you entered.

    ``mask`` restricts which rows contribute to each symbol's mean -- pass the
    tradability mask so untradable rows do not shift the baseline. A symbol with no
    usable rows yields all-NaN rather than silently contributing a zero baseline.
    """
    returns = np.asarray(returns, dtype=float)
    values = returns if mask is None else np.where(mask, returns, np.nan)
    usable = np.where(np.isfinite(values), values, np.nan)
    # An all-NaN column is a legitimate outcome (a symbol with nothing tradable);
    # nanmean warns about it, and NaN is exactly the answer we want propagated.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        means = np.nanmean(usable, axis=0, keepdims=True)
    return returns - means


def block_stats(
    values: np.ndarray,
    time_index: np.ndarray,
    block_size: int,
    *,
    min_blocks: int = 8,
) -> BlockStat:
    """Mean and t-statistic over non-overlapping, cross-sectionally averaged blocks.

    ``values`` and ``time_index`` are parallel 1-D arrays: one entry per observation,
    with ``time_index`` giving the row of the grid the observation came from.
    ``block_size`` should be the forward horizon in grid steps, so that no two blocks
    share any part of their return window.

    Blocks are the fixed partition ``time_index // block_size`` rather than a greedy
    walk. That choice matters when comparing subsets: with per-subset greedy sampling
    each subset lands on different timestamps, and the combined group stops being a
    weighted average of its parts. In the 2026-09-11 analysis that produced an
    apparently significant +22.1 bps (t=+2.48) for the combined group whose own halves
    were +6.7 and -5.9; on the shared partition it was -3.3 bps (t=-1.26). A fixed
    partition keeps every subset on the same blocks and the contradiction cannot arise.

    ``t_stat`` is None when fewer than ``min_blocks`` blocks survive -- with a handful
    of blocks the t-distribution is so wide that reporting a number invites exactly
    the over-reading it is meant to prevent.
    """
    if block_size < 1:
        raise ValueError("block_size must be at least 1")
    values = np.asarray(values, dtype=float).ravel()
    time_index = np.asarray(time_index).ravel()
    if values.shape != time_index.shape:
        raise ValueError("values and time_index must have the same length")

    finite = np.isfinite(values)
    values, time_index = values[finite], time_index[finite]
    if values.size == 0:
        return BlockStat(mean=float("nan"), t_stat=None, n_observations=0, n_blocks=0)

    # Average within a block first: 46 correlated symbols in one block are closer to
    # a single observation than to 46 independent ones.
    blocks = time_index // block_size
    order = np.argsort(blocks, kind="stable")
    blocks, sorted_values = blocks[order], values[order]
    edges = np.flatnonzero(np.diff(blocks)) + 1
    block_means = np.array([g.mean() for g in np.split(sorted_values, edges)])

    n_blocks = int(block_means.size)
    mean = float(values.mean())
    block_mean = float(block_means.mean())
    if n_blocks < max(min_blocks, 2):
        return BlockStat(
            mean=mean,
            t_stat=None,
            n_observations=int(values.size),
            n_blocks=n_blocks,
            block_mean=block_mean,
        )

    std = float(block_means.std(ddof=1))
    if std == 0.0:
        t_stat = None
    else:
        t_stat = float(block_mean / (std / np.sqrt(n_blocks)))
    return BlockStat(
        mean=mean,
        t_stat=t_stat,
        n_observations=int(values.size),
        n_blocks=n_blocks,
        block_mean=block_mean,
    )


def naive_t_stat(values: np.ndarray) -> float:
    """Treat every observation as independent. Present only as a comparison.

    ``test_forward.py`` pins how badly this overstates significance on overlapping,
    cross-correlated data. Do not use it to make a decision; it is here so the gap
    between it and :func:`block_stats` stays visible and tested.
    """
    values = np.asarray(values, dtype=float).ravel()
    values = values[np.isfinite(values)]
    if values.size < 2:
        return float("nan")
    return float(values.mean() / (values.std(ddof=1) / np.sqrt(values.size)))
