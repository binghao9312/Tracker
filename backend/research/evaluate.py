"""Evaluate research signals with response curves, rank IC, and barrier backtests.

A price grid shows one price per step. A barrier touched and retraced between two
samples is invisible, which biases results optimistically. Fills are assumed immediate
and at the mid. There is no latency and no spread.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

import numpy as np

from research.forward import BlockStat, block_stats, demean_by_symbol, forward_returns


@dataclass(frozen=True)
class Bucket:
    index: int
    lower: float
    upper: float
    stat: BlockStat


@dataclass(frozen=True)
class ResponseCurve:
    feature: str
    horizon_seconds: int
    buckets: tuple[Bucket, ...]

    def describe(self) -> str:
        lines = [f"{self.feature} response ({self.horizon_seconds}s)"]
        for bucket in self.buckets:
            lines.append(
                f"  {bucket.index}: [{bucket.lower:.6g}, {bucket.upper:.6g}] "
                f"{bucket.stat.describe()}"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class ICResult:
    feature: str
    horizon_seconds: int
    mean_ic: float
    n_symbols: int
    positive_symbols: int
    n_observations: int
    per_symbol: Mapping[str, float]


@dataclass(frozen=True)
class BacktestConfig:
    entry_score: float = 80.0
    take_profit_pct: float = 2.0
    stop_loss_pct: float = 1.0
    max_holding_seconds: int = 3600
    cost_bps: float = 11.4
    max_open_positions: int = 3
    control_seed: int = 0


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: str
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    exit_reason: str
    gross_bps: float
    net_bps: float


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[Trade, ...]
    control: tuple[Trade, ...]
    stat: BlockStat
    control_stat: BlockStat

    def describe(self) -> str:
        return (
            f"signal: {self.stat.describe()}\n"
            f"control: {self.control_stat.describe()}\n"
            f"trades={len(self.trades)}"
        )


def _horizon_steps(panel: object, horizon_seconds: int) -> int:
    if horizon_seconds < 1:
        raise ValueError("horizon_seconds must be at least 1")
    step_seconds = int(panel.step_seconds)
    if step_seconds < 1:
        raise ValueError("panel.step_seconds must be positive")
    if horizon_seconds % step_seconds:
        raise ValueError("horizon_seconds must be a multiple of panel.step_seconds")
    return horizon_seconds // step_seconds


def _rankdata(values: np.ndarray) -> np.ndarray:
    """Return one-based average ranks, including average ranks for ties."""
    values = np.asarray(values, dtype=float)
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    ranks = np.empty(values.size, dtype=float)
    starts = np.r_[0, np.flatnonzero(np.diff(sorted_values)) + 1]
    ends = np.r_[starts[1:], values.size]
    for start, end in zip(starts, ends, strict=False):
        ranks[order[start:end]] = (start + end + 1) / 2.0
    return ranks


def _spearman(feature: np.ndarray, returns: np.ndarray) -> float:
    if feature.size < 3:
        return float("nan")
    feature_ranks = _rankdata(feature)
    return_ranks = _rankdata(returns)
    feature_ranks -= feature_ranks.mean()
    return_ranks -= return_ranks.mean()
    denominator = float(
        np.sqrt(np.dot(feature_ranks, feature_ranks) * np.dot(return_ranks, return_ranks))
    )
    if denominator == 0.0:
        return float("nan")
    return float(np.dot(feature_ranks, return_ranks) / denominator)


def rank_ic(
    panel: object,
    feature: str,
    horizon_seconds: int,
    *,
    within_symbol: bool = True,
    absolute: bool = True,
    tradable_only: bool = True,
) -> ICResult:
    """Measure Spearman correlation between a feature and forward returns."""
    horizon_steps = _horizon_steps(panel, horizon_seconds)
    feature_values = np.asarray(panel.feature(feature), dtype=float)
    prices = np.asarray(panel.feature("price"), dtype=float)
    returns = forward_returns(prices, horizon_steps)
    if absolute:
        returns = np.abs(returns)

    tradable = np.asarray(panel.tradable, dtype=bool)
    entry_mask = tradable if tradable_only else np.ones_like(tradable, dtype=bool)
    valid = np.isfinite(feature_values) & np.isfinite(returns) & entry_mask

    per_symbol: dict[str, float] = {}
    observation_counts: dict[str, int] = {}
    for symbol_index, symbol in enumerate(panel.symbols):
        mask = valid[:, symbol_index]
        count = int(mask.sum())
        if count < 3:
            continue
        correlation = _spearman(feature_values[mask, symbol_index], returns[mask, symbol_index])
        if np.isfinite(correlation):
            per_symbol[symbol] = correlation
            observation_counts[symbol] = count

    if within_symbol:
        correlations = np.asarray(tuple(per_symbol.values()), dtype=float)
        mean_ic = float(correlations.mean()) if correlations.size else float("nan")
        n_observations = sum(observation_counts.values())
        n_symbols = len(per_symbol)
    else:
        qualified = np.zeros_like(valid, dtype=bool)
        for symbol_index, symbol in enumerate(panel.symbols):
            if symbol in observation_counts:
                qualified[:, symbol_index] = valid[:, symbol_index]
        pooled_feature = feature_values[qualified]
        pooled_returns = returns[qualified]
        mean_ic = (
            _spearman(pooled_feature, pooled_returns) if pooled_feature.size >= 3 else float("nan")
        )
        n_observations = int(qualified.sum())
        n_symbols = len(per_symbol)

    positive_symbols = sum(value > 0.0 for value in per_symbol.values())
    return ICResult(
        feature=feature,
        horizon_seconds=horizon_seconds,
        mean_ic=mean_ic,
        n_symbols=n_symbols,
        positive_symbols=positive_symbols,
        n_observations=n_observations,
        per_symbol=per_symbol,
    )


def response_curve(
    panel: object,
    feature: str,
    horizon_seconds: int,
    *,
    buckets: int = 10,
    tradable_only: bool = True,
    demean: bool = True,
) -> ResponseCurve:
    """Group feature observations into quantile buckets and compute block statistics."""
    if buckets < 1:
        raise ValueError("buckets must be at least 1")
    horizon_steps = _horizon_steps(panel, horizon_seconds)
    feature_values = np.asarray(panel.feature(feature), dtype=float)
    prices = np.asarray(panel.feature("price"), dtype=float)
    returns = forward_returns(prices, horizon_steps)
    tradable = np.asarray(panel.tradable, dtype=bool)
    entry_mask = tradable if tradable_only else np.ones_like(tradable, dtype=bool)
    if demean:
        returns = demean_by_symbol(returns, entry_mask)
    valid = np.isfinite(feature_values) & np.isfinite(returns) & entry_mask
    values = feature_values[valid]
    if values.size == 0:
        return ResponseCurve(feature, horizon_seconds, ())

    edges = np.quantile(values, np.linspace(0.0, 1.0, buckets + 1))
    bucket_indices = np.searchsorted(edges[1:-1], values, side="right")
    times = np.broadcast_to(np.arange(feature_values.shape[0])[:, None], feature_values.shape)[
        valid
    ]
    return ResponseCurve(
        feature=feature,
        horizon_seconds=horizon_seconds,
        buckets=tuple(
            Bucket(
                index=index,
                lower=float(edges[index]),
                upper=float(edges[index + 1]),
                stat=block_stats(
                    returns[valid][bucket_indices == index],
                    times[bucket_indices == index],
                    horizon_steps,
                ),
            )
            for index in range(buckets)
        ),
    )


def _trade_from_path(
    *,
    symbol: str,
    side: str,
    entry_index: int,
    prices: np.ndarray,
    horizon_steps: int,
    config: BacktestConfig,
) -> Trade | None:
    entry_price = float(prices[entry_index])
    if not isfinite(entry_price) or entry_price <= 0.0:
        return None
    if side == "LONG":
        target = entry_price * (1.0 + config.take_profit_pct / 100.0)
        stop = entry_price * (1.0 - config.stop_loss_pct / 100.0)
    else:
        target = entry_price * (1.0 - config.take_profit_pct / 100.0)
        stop = entry_price * (1.0 + config.stop_loss_pct / 100.0)

    last_finite_index: int | None = None
    end_index = min(entry_index + horizon_steps, len(prices) - 1)
    for exit_index in range(entry_index + 1, end_index + 1):
        price = float(prices[exit_index])
        if not isfinite(price) or price <= 0.0:
            continue
        last_finite_index = exit_index
        if side == "LONG":
            if price <= stop:
                reason = "STOP_LOSS"
            elif price >= target:
                reason = "TAKE_PROFIT"
            else:
                continue
        else:
            if price >= stop:
                reason = "STOP_LOSS"
            elif price <= target:
                reason = "TAKE_PROFIT"
            else:
                continue
        gross_bps = (price / entry_price - 1.0) * 10_000.0
        if side == "SHORT":
            gross_bps = -gross_bps
        return Trade(
            symbol=symbol,
            side=side,
            entry_index=entry_index,
            exit_index=exit_index,
            entry_price=entry_price,
            exit_price=price,
            exit_reason=reason,
            gross_bps=float(gross_bps),
            net_bps=float(gross_bps - config.cost_bps),
        )

    if last_finite_index is None:
        return None
    price = float(prices[last_finite_index])
    gross_bps = (price / entry_price - 1.0) * 10_000.0
    if side == "SHORT":
        gross_bps = -gross_bps
    return Trade(
        symbol=symbol,
        side=side,
        entry_index=entry_index,
        exit_index=last_finite_index,
        entry_price=entry_price,
        exit_price=price,
        exit_reason="TIME_STOP",
        gross_bps=float(gross_bps),
        net_bps=float(gross_bps - config.cost_bps),
    )


def _validate_backtest_config(config: BacktestConfig) -> None:
    if not isfinite(config.entry_score):
        raise ValueError("entry_score must be finite")
    if not isfinite(config.take_profit_pct) or config.take_profit_pct < 0.0:
        raise ValueError("take_profit_pct must be finite and non-negative")
    if not isfinite(config.stop_loss_pct) or config.stop_loss_pct < 0.0:
        raise ValueError("stop_loss_pct must be finite and non-negative")
    if not isfinite(config.cost_bps):
        raise ValueError("cost_bps must be finite")
    if config.max_open_positions < 1:
        raise ValueError("max_open_positions must be at least 1")
    if config.max_holding_seconds < 1:
        raise ValueError("max_holding_seconds must be at least 1")


def barrier_backtest(panel: object, config: BacktestConfig | None = None) -> BacktestResult:
    """Run a global-cap, one-position-per-symbol barrier simulation on a panel."""
    config = config or BacktestConfig()
    _validate_backtest_config(config)
    horizon_steps = _horizon_steps(panel, config.max_holding_seconds)
    prices = np.asarray(panel.feature("price"), dtype=float)
    scores = np.asarray(panel.feature("activity_score"), dtype=float)
    biases = np.asarray(panel.feature("bias"), dtype=float)
    tradable = np.asarray(panel.tradable, dtype=bool)
    steps, symbol_count = prices.shape
    rng = np.random.default_rng(config.control_seed)

    # Control candidates are found on demand, per (symbol, side) that actually trades.
    # Enumerating them all up front costs one simulated path per tradable row per side
    # -- around 795,000 of them on a 24h panel -- and the live entry threshold has never
    # fired, so the usual case paid all of that to produce nothing. Laziness does not
    # perturb the draws: the generator is still consumed exactly once per signal trade,
    # in trade order, so a given seed yields the same controls as before.
    control_rows: dict[tuple[int, str], np.ndarray] = {}

    def control_candidates(symbol_index: int, side: str) -> np.ndarray:
        cached = control_rows.get((symbol_index, side))
        if cached is not None:
            return cached
        candidates = [
            int(index)
            for index in np.flatnonzero(tradable[:, symbol_index])
            if _trade_from_path(
                symbol=panel.symbols[symbol_index],
                side=side,
                entry_index=int(index),
                prices=prices[:, symbol_index],
                horizon_steps=horizon_steps,
                config=config,
            )
            is not None
        ]
        found = np.asarray(candidates, dtype=int)
        control_rows[(symbol_index, side)] = found
        return found

    trades: list[Trade] = []
    controls: list[Trade] = []
    open_positions: dict[str, Trade] = {}
    for entry_index in range(steps):
        for symbol, trade in tuple(open_positions.items()):
            if trade.exit_index <= entry_index:
                del open_positions[symbol]
        if len(open_positions) >= config.max_open_positions:
            continue
        for symbol_index, symbol in enumerate(panel.symbols):
            if len(open_positions) >= config.max_open_positions:
                break
            if symbol in open_positions:
                continue
            score = scores[entry_index, symbol_index]
            bias = biases[entry_index, symbol_index]
            if (
                not tradable[entry_index, symbol_index]
                or not isfinite(float(score))
                or not isfinite(float(bias))
                or score < config.entry_score
                or bias == 0.0
            ):
                continue
            side = "LONG" if bias > 0.0 else "SHORT"
            trade = _trade_from_path(
                symbol=symbol,
                side=side,
                entry_index=entry_index,
                prices=prices[:, symbol_index],
                horizon_steps=horizon_steps,
                config=config,
            )
            if trade is None:
                continue
            trades.append(trade)
            open_positions[symbol] = trade

            candidates = control_candidates(symbol_index, side)
            if candidates.size:
                control_entry = int(rng.choice(candidates))
                control_trade = _trade_from_path(
                    symbol=symbol,
                    side=side,
                    entry_index=control_entry,
                    prices=prices[:, symbol_index],
                    horizon_steps=horizon_steps,
                    config=config,
                )
                if control_trade is not None:
                    controls.append(control_trade)

    signal_values = np.asarray([trade.net_bps for trade in trades], dtype=float)
    signal_times = np.asarray([trade.entry_index for trade in trades], dtype=int)
    control_values = np.asarray([trade.net_bps for trade in controls], dtype=float)
    control_times = np.asarray([trade.entry_index for trade in controls], dtype=int)
    stat = block_stats(signal_values, signal_times, horizon_steps)
    control_stat = block_stats(control_values, control_times, horizon_steps)
    return BacktestResult(tuple(trades), tuple(controls), stat, control_stat)
