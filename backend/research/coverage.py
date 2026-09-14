"""Refuse incomplete or regime-narrow market data before research uses it."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median
from typing import Any

from sqlalchemy import case, func, select

from app.database import MarketMetricRow


@dataclass(frozen=True)
class DayCoverage:
    day: str
    symbols: int
    rows: int
    max_gap_seconds: float
    range_pct: float
    research_grade: bool
    reason: str


@dataclass(frozen=True)
class CoverageReport:
    days: tuple[DayCoverage, ...]
    usable_days: int
    regime_spread: float

    def verdict(self, *, min_days: int, min_regime_spread: float) -> tuple[bool, str]:
        failures: list[str] = []
        if self.usable_days < min_days:
            failures.append(f"usable days {self.usable_days} < min_days {min_days}")
        if self.regime_spread < min_regime_spread:
            failures.append(
                f"regime spread {self.regime_spread:g} < min_regime_spread {min_regime_spread:g}"
            )
        if failures:
            return False, "; ".join(failures)
        return True, "coverage passes"


def _day_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _timestamp_day(value: datetime) -> str:
    return value.date().isoformat()


def _format_number(value: float) -> str:
    return f"{value:g}"


async def coverage_report(
    session_factory,
    *,
    start: datetime,
    end: datetime,
    reference_exchange: str = "binance",
    reference_market: str = "perp",
    # The reference feed (binance perp) carries exactly 40 symbols, so a threshold of
    # 40 would refuse a whole day over one delisting or one late-starting stream. 36
    # still catches a real collapse of the universe without tripping on ordinary churn.
    min_symbols: int = 36,
    max_gap_seconds: float = 120.0,
) -> CoverageReport:
    """Measure and classify each populated UTC calendar day in a data window."""
    day_expression = func.date(MarketMetricRow.timestamp)
    filters = (
        MarketMetricRow.timestamp >= start,
        MarketMetricRow.timestamp <= end,
        MarketMetricRow.exchange == reference_exchange,
        MarketMetricRow.market == reference_market,
    )

    symbol_count = func.count(func.distinct(MarketMetricRow.symbol)).label("symbols")
    row_count = func.count(MarketMetricRow.id).label("rows")
    density_statement = (
        select(day_expression.label("day"), symbol_count, row_count)
        .where(*filters)
        .group_by(day_expression)
        .order_by(day_expression)
    )

    average_price = func.avg(MarketMetricRow.price)
    range_expression = case(
        (
            average_price != 0,
            (func.max(MarketMetricRow.price) - func.min(MarketMetricRow.price))
            / average_price
            * 100,
        ),
        else_=0.0,
    ).label("range_pct")
    regime_statement = (
        select(day_expression.label("day"), MarketMetricRow.symbol, range_expression)
        .where(*filters)
        .group_by(day_expression, MarketMetricRow.symbol)
        .order_by(day_expression, MarketMetricRow.symbol)
    )

    timestamp_statement = (
        select(MarketMetricRow.timestamp)
        .where(*filters)
        .distinct()
        .order_by(MarketMetricRow.timestamp)
    )

    async with session_factory() as session:
        density_rows = (await session.execute(density_statement)).all()
        regime_rows = (await session.execute(regime_statement)).all()
        timestamp_rows = (await session.execute(timestamp_statement)).all()

    ranges_by_day: dict[str, list[float]] = {}
    for row in regime_rows:
        ranges_by_day.setdefault(_day_key(row.day), []).append(float(row.range_pct or 0.0))

    max_gaps: dict[str, float] = {}
    previous_by_day: dict[str, datetime] = {}
    for (timestamp,) in timestamp_rows:
        day = _timestamp_day(timestamp)
        previous = previous_by_day.get(day)
        if previous is not None:
            gap = (timestamp - previous).total_seconds()
            if gap > max_gaps.get(day, 0.0):
                max_gaps[day] = gap
        previous_by_day[day] = timestamp

    days: list[DayCoverage] = []
    for row in density_rows:
        day = _day_key(row.day)
        symbols = int(row.symbols)
        rows = int(row.rows)
        max_gap = max_gaps.get(day, 0.0)
        reasons: list[str] = []
        if symbols < min_symbols:
            reasons.append(f"symbols {symbols} < minimum {min_symbols}")
        if max_gap > max_gap_seconds:
            reasons.append(
                f"max gap {_format_number(max_gap)}s > {_format_number(max_gap_seconds)}s"
            )
        days.append(
            DayCoverage(
                day=day,
                symbols=symbols,
                rows=rows,
                max_gap_seconds=max_gap,
                range_pct=float(median(ranges_by_day.get(day, [0.0]))),
                research_grade=not reasons,
                reason="; ".join(reasons),
            )
        )

    ordered_days = tuple(days)
    usable_ranges = [day.range_pct for day in ordered_days if day.research_grade]
    if len(usable_ranges) < 2 or min(usable_ranges) == 0:
        spread = 0.0
    else:
        spread = max(usable_ranges) / min(usable_ranges)
    return CoverageReport(
        days=ordered_days,
        usable_days=len(usable_ranges),
        regime_spread=spread,
    )


def summarize(days: Sequence[DayCoverage]) -> str:
    """Render the daily refusal decisions and aggregate totals for a human reader."""
    rows = [
        "day | symbols | rows | max_gap_s | range_pct | grade | reason",
        "--- | ---: | ---: | ---: | ---: | --- | ---",
    ]
    for day in days:
        rows.append(
            f"{day.day} | {day.symbols} | {day.rows} | "
            f"{_format_number(day.max_gap_seconds)} | {_format_number(day.range_pct)} | "
            f"{'research' if day.research_grade else 'refused'} | {day.reason or 'ok'}"
        )

    usable_ranges = [day.range_pct for day in days if day.research_grade]
    if len(usable_ranges) < 2 or min(usable_ranges) == 0:
        spread = 0.0
    else:
        spread = max(usable_ranges) / min(usable_ranges)
    rows.append(
        f"totals | usable_days={len(usable_ranges)} | days={len(days)} | "
        f"regime_spread={_format_number(spread)}"
    )
    return "\n".join(rows)
