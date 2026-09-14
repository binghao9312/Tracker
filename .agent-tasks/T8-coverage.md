# Task T8: backend/research/coverage.py — the Phase C1 accumulation report

Create the module that `backend/tests/test_research_coverage.py` imports. That test
already exists and currently fails with `ModuleNotFoundError`. It is the complete
specification: read it first and in full, including its module docstring, which explains
why the report exists and what it is for.

The job of this report is to **refuse** data, not to describe it. A previous 38-hour
accumulation turned out to be unusable and nobody noticed until the analysis was already
built on it. A number here that is wrong in the optimistic direction wastes weeks.

## Hard constraints

- Create ONLY `backend/research/coverage.py`.
- Do NOT modify anything under `backend/tests/`, and do not modify any other file. If you
  believe a test is wrong, stop and say so instead of changing it. Two earlier tasks in
  this repo found real errors in my tests that way, so this is a genuine option.
- `backend/app/` must never import from `backend/research/`.
- **The SQL must run on both PostgreSQL and SQLite.** The tests use an in-memory SQLite
  database; production is Postgres. So no `date_trunc`, no `percentile_cont`, no
  `::` casts. `sqlalchemy.cast(column, sqlalchemy.Date)` and ordinary aggregates work on
  both; do any median in Python.
- No new third-party dependencies. `numpy` and `sqlalchemy` are available; no pandas.
- Do NOT run `uv`, `pip install`, or `poetry`, and do not create or modify lock files.

```scope
backend/research/coverage.py
```

## Context

**Every shell command must be non-interactive or it will hang forever.** `git` needs
`--no-pager`.

Run everything from `C:\Users\HAO\Documents\Qtrade\backend` using this interpreter:

```
./.venv/Scripts/python.exe
```

Read `backend/tests/research_helpers.py` for the row shapes and the in-memory database,
and `backend/research/panel.py` for how this repo queries with an async session factory.

## Required interface

Field order matters: the tests construct `DayCoverage` positionally.

```python
@dataclass(frozen=True)
class DayCoverage:
    day: str               # ISO date, e.g. "2026-09-13"
    symbols: int           # distinct symbols seen on the reference feed that day
    rows: int              # reference-feed rows that day
    max_gap_seconds: float # largest gap between consecutive reference-feed timestamps
    range_pct: float       # regime proxy; see below
    research_grade: bool
    reason: str            # why it was refused; empty when research_grade

@dataclass(frozen=True)
class CoverageReport:
    days: tuple[DayCoverage, ...]     # chronological
    usable_days: int
    regime_spread: float

    def verdict(self, *, min_days: int, min_regime_spread: float) -> tuple[bool, str]: ...

async def coverage_report(
    session_factory, *, start: datetime, end: datetime,
    reference_exchange: str = "binance", reference_market: str = "perp",
    min_symbols: int = 40, max_gap_seconds: float = 120.0,
) -> CoverageReport: ...

def summarize(days: Sequence[DayCoverage]) -> str: ...
```

## Semantics

**Reference feed.** All density and gap figures come from `market_metrics` rows matching
`reference_exchange` / `reference_market`. Gaps are between *distinct* timestamps on that
feed within the day, not per symbol — every symbol is written on the same cadence tick,
so per-symbol gaps would just multiply the same number.

**Refusal.** A day is research-grade only if it has at least `min_symbols` distinct
symbols and its largest gap is at most `max_gap_seconds`. When refused, `reason` says
which test failed and quotes the offending number; the tests check that the word "gap" or
"symbol" appears and that `summarize` reproduces the figure.

**Regime proxy.** Per symbol per day compute `(max(price) - min(price)) / avg(price) *
100`, then take the **median across symbols** for that day. Crude on purpose: it is a
pure SQL aggregate, so it costs one pass over a month of rows, where anything based on
returns would need the rows themselves.

**Regime spread.** `max(range_pct) / min(range_pct)` over the **research-grade days
only** — a broken day can look wildly volatile and must not be allowed to count as a
second regime. With fewer than two usable days, or a zero minimum, the spread is `0.0`.

**Verdict.** Passes when `usable_days >= min_days` **and**
`regime_spread >= min_regime_spread`. Both are required: three weeks drawn from one quiet
stretch does not satisfy Phase C1, and a day count alone would let that pass silently.
When it fails, the returned string must name the threshold that was missed, including its
numeric value.

## Also required

`summarize` returns a plain-text table, one line per day, ending with the totals. It is
what a human reads each morning to decide whether yesterday counted, so put the refusal
reason on the line rather than in a footnote.

## Acceptance

```acceptance
cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_research_coverage.py -q
cd backend && ./.venv/Scripts/python.exe -m pytest -q
cd backend && ./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m ruff format --check .
```

All three must exit 0. The first currently fails at import with
`ModuleNotFoundError: No module named 'research.coverage'`.

Report at the end:
1. The `pytest -q` summary line for the whole suite.
2. Any test whose intent you found ambiguous, and how you resolved it.
3. Anything in the oracle that looks wrong to you.
