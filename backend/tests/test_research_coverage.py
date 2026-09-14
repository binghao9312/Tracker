"""Contract for the accumulation report that gates Phase C.

Phase C1 is a waiting task: it needs three to four weeks of data spanning more than one
volatility environment. Waiting is the failure mode. The last attempt accumulated 38
hours that turned out to be unusable, and nobody knew until the analysis was already
built on it -- the order book had been degrading for the whole period and the write gaps
grew from 13.8s to 278s without anything saying so.

So the point of this report is not to describe the data, it is to refuse it. Two
questions have to be answerable on any given day without re-deriving them:

* Is what we are collecting research-grade right now? A day whose feeds gapped, or whose
  universe collapsed, is not partial evidence -- it is a day that must be excluded, and
  excluded days do not count towards the three weeks.
* Have we actually seen different regimes? "Three weeks of data" drawn entirely from one
  quiet stretch does not satisfy C1, and reporting a day count alone would let that pass
  silently. The spread between the calmest and busiest usable day is the cheapest honest
  answer, so it is computed rather than eyeballed.

The volatility proxy is the daily high-low range over the mid, taken per symbol and then
median-aggregated across the universe. It is deliberately crude: it needs one pass of
min/max/avg per symbol per day, which is a pure SQL aggregate over a month of rows,
where anything based on returns would need the rows themselves.
"""

from __future__ import annotations

import unittest

from research.coverage import DayCoverage, coverage_report, summarize
from tests.research_helpers import (
    at,
    insert,
    market_row,
    memory_session_factory,
)

DAY = 86_400


def _day(
    *,
    index: int,
    symbols: int = 46,
    per_hour: int = 1_800,
    gap_at: float | None = None,
    low: float = 100.0,
    high: float = 101.0,
) -> list[dict]:
    """One synthetic day of reference-feed rows for ``symbols`` symbols.

    ``per_hour`` sets the cadence, ``gap_at`` injects a stall of that many seconds, and
    ``low``/``high`` set the price range the regime proxy will read.
    """
    rows: list[dict] = []
    step = 3600 / per_hour
    base = index * DAY
    seconds = 0.0
    stalled = False
    while seconds < DAY:
        if gap_at is not None and not stalled and seconds >= DAY / 2:
            seconds += gap_at
            stalled = True
            continue
        for symbol_index in range(symbols):
            # Sweep the range so min and max are both reached within the day.
            fraction = (seconds % 3600) / 3600
            price = low + (high - low) * fraction
            rows.append(
                market_row(
                    seconds=base + seconds,
                    exchange="binance",
                    symbol=f"S{symbol_index:02d}USDT",
                    market="perp",
                    price=price,
                )
            )
        seconds += step
    return rows


class DayClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_healthy_day_is_research_grade(self) -> None:
        factory = memory_session_factory()
        insert(factory, "market_metrics", _day(index=0, per_hour=120))
        report = await coverage_report(factory, start=at(0), end=at(DAY))
        self.assertEqual(len(report.days), 1)
        day = report.days[0]
        self.assertTrue(day.research_grade, day.reason)
        self.assertEqual(day.symbols, 46)

    async def test_a_day_with_a_long_stall_is_refused(self) -> None:
        """The exact failure that wasted the previous accumulation."""
        factory = memory_session_factory()
        insert(factory, "market_metrics", _day(index=0, per_hour=120, gap_at=600))
        report = await coverage_report(factory, start=at(0), end=at(DAY))
        day = report.days[0]
        self.assertFalse(day.research_grade)
        self.assertIn("gap", day.reason.lower())
        self.assertGreaterEqual(day.max_gap_seconds, 600)

    async def test_a_day_missing_most_of_the_universe_is_refused(self) -> None:
        factory = memory_session_factory()
        insert(factory, "market_metrics", _day(index=0, symbols=5, per_hour=120))
        day = (await coverage_report(factory, start=at(0), end=at(DAY))).days[0]
        self.assertFalse(day.research_grade)
        self.assertIn("symbol", day.reason.lower())

    async def test_a_day_too_sparse_to_use_is_refused(self) -> None:
        """1,107 rows in a day is what the degraded period actually looked like."""
        factory = memory_session_factory()
        insert(factory, "market_metrics", _day(index=0, per_hour=2))
        day = (await coverage_report(factory, start=at(0), end=at(DAY))).days[0]
        self.assertFalse(day.research_grade)

    async def test_only_research_grade_days_count_towards_the_total(self) -> None:
        factory = memory_session_factory()
        rows = _day(index=0, per_hour=120) + _day(index=1, per_hour=120, gap_at=900)
        rows += _day(index=2, per_hour=120)
        insert(factory, "market_metrics", rows)
        report = await coverage_report(factory, start=at(0), end=at(3 * DAY))
        self.assertEqual(len(report.days), 3)
        self.assertEqual(report.usable_days, 2)


class RegimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_regime_spread_reflects_the_calmest_and_busiest_day(self) -> None:
        factory = memory_session_factory()
        rows = _day(index=0, per_hour=120, low=100.0, high=100.5)  # 0.5% range
        rows += _day(index=1, per_hour=120, low=100.0, high=102.0)  # 2.0% range
        insert(factory, "market_metrics", rows)
        report = await coverage_report(factory, start=at(0), end=at(2 * DAY))
        ranges = sorted(day.range_pct for day in report.days)
        # The sweep runs low + (high-low)*n/120 for n in 0..119, so it approaches `high`
        # without reaching it: max 100.49583 / 101.98333 against means of 100.24792 /
        # 100.99167. Ranges are therefore 0.49461% and 1.96386%, not 0.5% and 2%.
        self.assertAlmostEqual(ranges[0], 0.49461, places=4)
        self.assertAlmostEqual(ranges[1], 1.96386, places=4)
        # Roughly fourfold: enough to call these different environments.
        self.assertGreater(report.regime_spread, 3.5)

    async def test_one_flat_stretch_does_not_satisfy_the_gate(self) -> None:
        """Three weeks drawn from a single quiet regime is the trap C1 guards against."""
        factory = memory_session_factory()
        rows: list[dict] = []
        for index in range(4):
            rows += _day(index=index, per_hour=120, low=100.0, high=100.5)
        insert(factory, "market_metrics", rows)
        report = await coverage_report(factory, start=at(0), end=at(4 * DAY))
        self.assertEqual(report.usable_days, 4)
        self.assertLess(report.regime_spread, 1.2, "identical days cannot span regimes")
        passed, _ = report.verdict(min_days=3, min_regime_spread=2.0)
        self.assertFalse(passed, "day count alone must not open the gate")

    async def test_excluded_days_do_not_contribute_to_the_regime_spread(self) -> None:
        """A broken day may look wildly volatile; it must not count as a regime."""
        factory = memory_session_factory()
        rows = _day(index=0, per_hour=120, low=100.0, high=100.5)
        rows += _day(index=1, per_hour=120, low=100.0, high=100.5)
        rows += _day(index=2, per_hour=120, gap_at=900, low=100.0, high=140.0)
        insert(factory, "market_metrics", rows)
        report = await coverage_report(factory, start=at(0), end=at(3 * DAY))
        self.assertEqual(report.usable_days, 2)
        self.assertLess(report.regime_spread, 1.2)


class VerdictTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_gate_needs_both_duration_and_spread(self) -> None:
        factory = memory_session_factory()
        rows = _day(index=0, per_hour=120, low=100.0, high=100.5)
        rows += _day(index=1, per_hour=120, low=100.0, high=102.0)
        rows += _day(index=2, per_hour=120, low=100.0, high=101.0)
        insert(factory, "market_metrics", rows)
        report = await coverage_report(factory, start=at(0), end=at(3 * DAY))

        passed, reason = report.verdict(min_days=3, min_regime_spread=2.0)
        self.assertTrue(passed, reason)
        passed, reason = report.verdict(min_days=21, min_regime_spread=2.0)
        self.assertFalse(passed)
        self.assertIn("21", reason)
        passed, reason = report.verdict(min_days=3, min_regime_spread=99.0)
        self.assertFalse(passed)

    async def test_an_empty_window_reports_nothing_rather_than_failing(self) -> None:
        factory = memory_session_factory()
        report = await coverage_report(factory, start=at(0), end=at(DAY))
        self.assertEqual(report.days, ())
        self.assertEqual(report.usable_days, 0)
        passed, _ = report.verdict(min_days=1, min_regime_spread=1.0)
        self.assertFalse(passed)


class SummaryTests(unittest.TestCase):
    def test_the_summary_names_why_each_day_was_refused(self) -> None:
        days = (
            DayCoverage("2026-09-12", 46, 1_107, 278.0, 1.2, False, "max gap 278s > 120s"),
            DayCoverage("2026-09-13", 46, 47_233, 3.9, 1.2, True, ""),
        )
        text = summarize(days)
        self.assertIn("2026-09-12", text)
        self.assertIn("278", text)
        self.assertIn("2026-09-13", text)


if __name__ == "__main__":
    unittest.main()
