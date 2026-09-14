"""One command per research question, so a hypothesis costs a line and not a script.

The reason this exists is not convenience. Between 2026-09-11 and 2026-09-14 the same
analysis was rewritten as an ad-hoc script six times, and two of those scripts were
wrong in ways that changed the answer: one multiplied basis points twice and reported
1883% alpha, another collapsed a per-symbol input to a single constant and moved the
cost estimate by 0.4 bps. Both were caught by the number looking implausible, which is
not a control. Every path through this module goes through the same panel and the same
statistics, so a mistake is made once and fixed once.

Commands:

    feed-health   per-feed write density; the Phase 0 diagnostic
    fidelity      does the offline rebuild still match what the runtime recorded?
    coverage      which accumulated days are usable, and do they span regimes
    evaluate      response curve and rank IC for one feature
    backtest      triple-barrier backtest of the live entry rule, against a control

``fidelity`` is the one to run first after any change to the runtime or to the panel.
The unit test of the same name proves the aggregation is faithful on synthetic rows
where the timestamps line up exactly; this command asks the harder question, on real
rows, where flow_metrics is stamped at persist time and market_metrics at source time
and the two therefore drift apart. Neither check subsumes the other.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import SignalMetricRow
from research.panel import build_panel

# The compose file publishes postgres on loopback only. Inside a container the host is
# "postgres" instead, which DATABASE_URL already carries.
DEFAULT_DATABASE_URL = "postgresql+asyncpg://qtrade:qtrade@127.0.0.1:5433/qtrade"

_HORIZON_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_horizon(text: str) -> int:
    """Turn "5m" into 300. Bare numbers are seconds."""
    text = text.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("empty horizon")
    if text[-1] in _HORIZON_UNITS:
        value, unit = text[:-1], _HORIZON_UNITS[text[-1]]
    else:
        value, unit = text, 1
    try:
        seconds = int(float(value) * unit)
    except ValueError:
        raise argparse.ArgumentTypeError(f"cannot read horizon {text!r}") from None
    if seconds < 1:
        raise argparse.ArgumentTypeError(f"horizon must be at least a second: {text!r}")
    return seconds


def parse_horizons(text: str) -> list[int]:
    return [parse_horizon(part) for part in text.split(",") if part.strip()]


def parse_time(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _window(args) -> tuple[datetime | None, datetime | None]:
    """Resolve --start/--end/--hours. ``None`` end means "the latest row in the table"."""
    if args.start is not None or args.end is not None:
        start = args.start
        end = args.end
        if start is None and end is not None:
            start = end - timedelta(hours=args.hours)
        return start, end
    return None, None


async def _resolve_window(session_factory, args) -> tuple[datetime, datetime]:
    start, end = _window(args)
    if end is None:
        async with session_factory() as session:
            latest = (await session.execute(select(func.max(SignalMetricRow.timestamp)))).scalar()
        if latest is None:
            raise SystemExit("no signal_metrics rows: is the collector running?")
        end = latest if latest.tzinfo is not None else latest.replace(tzinfo=UTC)
    if start is None:
        start = end - timedelta(hours=args.hours)
    if start >= end:
        raise SystemExit(f"empty window: {start.isoformat()} .. {end.isoformat()}")
    return start, end


def _session_factory(url: str):
    engine = create_async_engine(url)
    return async_sessionmaker(engine, expire_on_commit=False), engine


def _symbols(args) -> tuple[str, ...] | None:
    if not args.symbols:
        return None
    return tuple(part.strip().upper() for part in args.symbols.split(",") if part.strip())


async def _build(session_factory, args, **extra):
    start, end = await _resolve_window(session_factory, args)
    print(
        f"window {start.isoformat(timespec='seconds')} .. {end.isoformat(timespec='seconds')} "
        f"step {args.step}s",
        file=sys.stderr,
    )
    panel = await build_panel(
        session_factory,
        start=start,
        end=end,
        step_seconds=args.step,
        symbols=_symbols(args),
        **extra,
    )
    print(
        f"panel {panel.shape[0]} x {panel.shape[1]} symbols, "
        f"{int(np.isfinite(panel.feature('price')).sum()):,} priced cells",
        file=sys.stderr,
    )
    return panel


# ---------------------------------------------------------------------------------
# fidelity
# ---------------------------------------------------------------------------------


async def command_fidelity(args) -> int:
    """Compare the rebuilt activity_score against what the runtime stored at the time.

    Exact agreement is not the bar and never could be: the runtime pairs a flow window
    computed at wall-clock now with whatever book it happened to be holding, while the
    panel pairs the newest row of each on a shared grid. What matters is that the
    disagreement stays small enough that conclusions drawn from the panel are
    conclusions about the live system.
    """
    session_factory, engine = _session_factory(args.database_url)
    try:
        panel = await _build(session_factory, args)
        if panel.shape[1] == 0:
            raise SystemExit("no symbols in the window")
        start, end = panel.grid[0], panel.grid[-1]
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        SignalMetricRow.timestamp,
                        SignalMetricRow.symbol,
                        SignalMetricRow.activity_score,
                    )
                    .where(
                        SignalMetricRow.timestamp
                        >= datetime.fromtimestamp(int(start), UTC) - timedelta(seconds=30),
                        SignalMetricRow.timestamp <= datetime.fromtimestamp(int(end), UTC),
                    )
                    .order_by(SignalMetricRow.timestamp)
                )
            ).all()
    finally:
        await engine.dispose()

    index = {symbol: position for position, symbol in enumerate(panel.symbols)}
    online: dict[int, dict[int, float]] = {position: {} for position in index.values()}
    for timestamp, symbol, score in rows:
        position = index.get(symbol)
        if position is None or score is None:
            continue
        moment = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)
        online[position][int(moment.timestamp())] = float(score)

    rebuilt = panel.feature("activity_score")
    differences: list[float] = []
    for position in index.values():
        stamps = sorted(online[position])
        if not stamps:
            continue
        stamp_array = np.array(stamps)
        for time_index, epoch in enumerate(panel.grid):
            value = rebuilt[time_index, position]
            if not np.isfinite(value):
                continue
            # The row the runtime wrote most recently at or before this grid point.
            slot = int(np.searchsorted(stamp_array, epoch, side="right")) - 1
            if slot < 0 or epoch - stamp_array[slot] > 30:
                continue
            differences.append(abs(float(value) - online[position][int(stamp_array[slot])]))

    if not differences:
        raise SystemExit("no overlapping cells to compare")
    diffs = np.array(differences)
    median = float(np.percentile(diffs, 50))
    print(f"compared {diffs.size:,} cells against signal_metrics")
    for label, value in (
        ("p50", median),
        ("p90", float(np.percentile(diffs, 90))),
        ("p99", float(np.percentile(diffs, 99))),
        ("max", float(diffs.max())),
    ):
        print(f"  |rebuilt - online| {label} = {value:.4f} score points")
    print(f"  within {args.tolerance}: {float((diffs < args.tolerance).mean()):.2%}")

    # The gate is on the median, not on the tail, and the reason is worth stating.
    # The tail is irreducible: flow_metrics is stamped when the row was written and
    # market_metrics when the exchange produced the book, so on a shared grid the panel
    # can pair a flow window with a different tick's depth than the runtime used. Since
    # depth is the denominator of the pressure term, and pressure is 50 of the 100
    # points, fast symbols disagree by several points a few percent of the time.
    # Measured 2026-09-14 over 8,280 cells across 46 symbols: p50 0.01, p90 0.47,
    # p99 3.27, and 94% of the disagreements above one point had identical
    # cross_exchange_state and oi_change_5m, which places them in the pressure term.
    #
    # A median that stays near zero is what says the aggregation itself is still right,
    # and it is a sharp drift detector precisely because it normally sits at 0.01.
    if median > args.max_median:
        print(
            f"FAIL: median disagreement {median:.4f} exceeds {args.max_median} score points. "
            "That is aggregation drift, not clock skew -- skew shows up in the tail and "
            "leaves the median near zero. The offline rebuild no longer describes the "
            "running system.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: median {median:.4f} <= {args.max_median}; the rebuild tracks the runtime.")
    print(
        "  Note: the tail is expected. Raw per-venue columns are exact -- they are read "
        "straight from the rows -- so component tests are unaffected. Only the derived "
        "composites (activity_score, bias) carry this pairing noise."
    )
    return 0


# ---------------------------------------------------------------------------------
# evaluate / backtest
# ---------------------------------------------------------------------------------


async def command_evaluate(args) -> int:
    from research.evaluate import rank_ic, response_curve

    session_factory, engine = _session_factory(args.database_url)
    try:
        panel = await _build(session_factory, args)
    finally:
        await engine.dispose()

    if args.feature not in panel.features:
        raise SystemExit(
            f"unknown feature {args.feature!r}; available: {', '.join(sorted(panel.features))}"
        )

    print(f"\n=== within-symbol rank IC: {args.feature} vs |forward return| ===")
    print(f"{'horizon':>8} {'mean IC':>9} {'symbols':>8} {'positive':>9} {'n':>10}")
    for horizon in args.horizons:
        result = rank_ic(panel, args.feature, horizon)
        print(
            f"{horizon:>7}s {result.mean_ic:>+9.4f} {result.n_symbols:>8} "
            f"{result.positive_symbols:>9} {result.n_observations:>10,}"
        )

    for horizon in args.horizons:
        print(f"\n=== response curve: {args.feature}, {horizon}s forward (symbol-demeaned) ===")
        curve = response_curve(panel, args.feature, horizon, buckets=args.buckets)
        print(f"{'bucket':>7} {'range':>26} {'forward return':>34}")
        for bucket in curve.buckets:
            span = f"[{bucket.lower:.4g}, {bucket.upper:.4g}]"
            print(f"{bucket.index:>7} {span:>26} {bucket.stat.describe():>34}")
    return 0


async def command_backtest(args) -> int:
    from research.evaluate import BacktestConfig, barrier_backtest

    session_factory, engine = _session_factory(args.database_url)
    try:
        panel = await _build(session_factory, args)
    finally:
        await engine.dispose()

    config = BacktestConfig(
        entry_score=args.entry_score,
        take_profit_pct=args.take_profit,
        stop_loss_pct=args.stop_loss,
        max_holding_seconds=args.max_holding,
        cost_bps=args.cost_bps,
        max_open_positions=args.max_open,
        control_seed=args.seed,
    )
    result = barrier_backtest(panel, config)
    print(f"\n=== triple-barrier backtest, entry score >= {config.entry_score} ===")
    print(f"cost {config.cost_bps} bps per round trip, TP {config.take_profit_pct}% / ", end="")
    print(f"SL {config.stop_loss_pct}%, max hold {config.max_holding_seconds}s")
    print(f"\n  signal  {len(result.trades):>5} trades   {result.stat.describe()}")
    print(f"  control {len(result.control):>5} trades   {result.control_stat.describe()}")
    if not result.trades:
        print(
            "\nNo trades. That is a finding, not a failure: the live threshold has never "
            "been reached in any sample measured so far.",
        )
        return 0
    reasons: dict[str, int] = {}
    for trade in result.trades:
        reasons[trade.exit_reason] = reasons.get(trade.exit_reason, 0) + 1
    print("\n  exits: " + ", ".join(f"{name} {count}" for name, count in sorted(reasons.items())))
    print(
        "\nThe control column is the comparison that matters. A signal group that does "
        "not beat entries drawn at random has shown nothing."
    )
    return 0


async def command_coverage(args) -> int:
    """Report which days of accumulated data are usable, and whether they span regimes.

    Phase C1 is satisfied by neither duration nor variety alone, so this refuses on
    both. Run it against the whole accumulation, not a recent slice -- the question is
    what the eventual analysis will be allowed to use.
    """
    from research.coverage import coverage_report, summarize

    session_factory, engine = _session_factory(args.database_url)
    try:
        start, end = await _resolve_window(session_factory, args)
        report = await coverage_report(
            session_factory,
            start=start,
            end=end,
            min_symbols=args.min_symbols,
            max_gap_seconds=args.max_gap,
        )
    finally:
        await engine.dispose()

    print(summarize(report.days))
    passed, reason = report.verdict(
        min_days=args.min_days, min_regime_spread=args.min_regime_spread
    )
    print()
    print(f"Phase C1 gate: {'PASS' if passed else 'NOT YET'} -- {reason}")
    return 0 if passed else 1


def command_feed_health(args) -> int:
    from research.feed_health import main as feed_health_main

    return feed_health_main(args.rest)


# ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m research", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy async URL (default: loopback postgres published by compose)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_window(sub):
        sub.add_argument("--hours", type=float, default=24.0, help="window length (default 24)")
        sub.add_argument("--start", type=parse_time, default=None)
        sub.add_argument("--end", type=parse_time, default=None, help="default: newest row")
        sub.add_argument("--step", type=int, default=10, help="grid step in seconds")
        sub.add_argument("--symbols", default=None, help="comma separated; default all")

    fidelity = subparsers.add_parser(
        "fidelity", help="check the offline rebuild against what the runtime recorded"
    )
    add_window(fidelity)
    # A shorter default than the other commands: this is a spot check, not an analysis,
    # and it is worth being cheap enough to run reflexively.
    fidelity.set_defaults(hours=1.0)
    fidelity.add_argument(
        "--tolerance", type=float, default=1.0, help="score points, for the reported share"
    )
    fidelity.add_argument(
        "--max-median",
        type=float,
        default=0.5,
        help="fail above this median disagreement (normally ~0.01)",
    )
    fidelity.set_defaults(run=command_fidelity, is_async=True)

    evaluate = subparsers.add_parser("evaluate", help="rank IC and response curve for a feature")
    add_window(evaluate)
    evaluate.add_argument("--feature", default="activity_score")
    evaluate.add_argument("--horizons", type=parse_horizons, default=[60, 300, 900, 3600])
    evaluate.add_argument("--buckets", type=int, default=10)
    evaluate.set_defaults(run=command_evaluate, is_async=True)

    backtest = subparsers.add_parser("backtest", help="triple-barrier backtest against a control")
    add_window(backtest)
    backtest.add_argument("--entry-score", type=float, default=80.0)
    backtest.add_argument("--take-profit", type=float, default=2.0)
    backtest.add_argument("--stop-loss", type=float, default=1.0)
    backtest.add_argument("--max-holding", type=int, default=3600)
    backtest.add_argument("--cost-bps", type=float, default=11.4)
    backtest.add_argument("--max-open", type=int, default=3)
    backtest.add_argument("--seed", type=int, default=0)
    backtest.set_defaults(run=command_backtest, is_async=True)

    coverage = subparsers.add_parser(
        "coverage", help="which accumulated days are usable, and do they span regimes"
    )
    add_window(coverage)
    coverage.set_defaults(hours=24.0 * 45)
    coverage.add_argument("--min-days", type=int, default=21, help="Phase C1 duration gate")
    coverage.add_argument(
        "--min-regime-spread",
        type=float,
        default=2.0,
        help="busiest usable day must be this many times the calmest",
    )
    coverage.add_argument(
        "--min-symbols",
        type=int,
        default=36,
        help="the reference feed carries 40, so this leaves room for ordinary churn",
    )
    coverage.add_argument("--max-gap", type=float, default=120.0, help="seconds")
    coverage.set_defaults(run=command_coverage, is_async=True)

    feed_health = subparsers.add_parser("feed-health", help="per-feed write density")
    feed_health.add_argument("rest", nargs=argparse.REMAINDER)
    feed_health.set_defaults(run=command_feed_health, is_async=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.is_async:
        return asyncio.run(args.run(args))
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
