# Task T5: backend/research/panel.py — feature panel rebuilt from the database

Create the module that `backend/tests/test_research_fidelity.py` imports. That test
already exists and currently fails with `ModuleNotFoundError`. It is the complete
specification: read it first and in full, including its module docstring, which states
the two aggregation rules that are easy to get backwards.

The panel is how every future research conclusion is computed. A wrong number here does
not crash — it silently changes what the system appears to have done. That is why the
oracle was written before the implementation.

## Hard constraints

- Create ONLY `backend/research/panel.py`.
- Do NOT modify anything under `backend/tests/`, and do not modify any other file. The
  tests are the oracle; editing them removes the only thing that can tell either of us
  the code is right. If you believe a test is wrong, stop and say so instead.
- `backend/app/` must never import from `backend/research/`. The dependency runs one
  way: research reads app, never the reverse.
- Do NOT copy logic out of `app/scoring.py` or `app/trade_signal.py`. Call the real
  functions. A second copy of the scoring arithmetic is the exact failure this whole
  test exists to prevent — it would drift from the live system and the drift would be
  invisible.
- No new third-party dependencies. `numpy` and `sqlalchemy` are already available; do
  NOT add pandas.
- Do NOT run `uv`, `pip install`, or `poetry`, and do not create or modify lock files.

```scope
backend/research/panel.py
```

## Context

**Every shell command must be non-interactive or it will hang forever.** `git` needs
`--no-pager`.

Run everything from `C:\Users\HAO\Documents\Qtrade\backend` using this interpreter (the
bare `python` on PATH is a different install and lacks the dependencies):

```
./.venv/Scripts/python.exe
```

Read these before writing anything:

- `backend/tests/test_research_fidelity.py` — the oracle, and the reference rebuild.
- `backend/tests/research_helpers.py` — the row shapes and the in-memory database.
- `backend/app/runtime.py`, specifically `_persist_flow` (line ~699) and `_build_detail`
  (line ~518). These are what the panel reconstructs. The reference in the test mirrors
  them, but reading the originals is worth the time.
- `backend/app/database.py` — the four tables involved: `market_metrics`,
  `flow_metrics`, `derivative_metrics`, `signal_metrics`.

## Required interface

```python
BIAS_CODES: Mapping[str, float]   # {"LONG": 1.0, "SHORT": -1.0, "NONE": 0.0}

@dataclass(frozen=True)
class Panel:
    grid: np.ndarray                    # (T,) int64 epoch seconds, evenly spaced
    symbols: tuple[str, ...]            # (S,) sorted
    features: Mapping[str, np.ndarray]  # each (T, S) float32
    step_seconds: int

    def feature(self, name: str) -> np.ndarray:      # KeyError if unknown
    def categorical(self, name: str) -> np.ndarray:  # (T, S) object array of str|None
    @property
    def tradable(self) -> np.ndarray:                # (T, S) bool
    @property
    def shape(self) -> tuple[int, int]:

async def build_panel(
    session_factory,
    *,
    start: datetime,
    end: datetime,
    step_seconds: int = 10,
    fresh_tolerance_seconds: int = 30,
    derivative_tolerance_seconds: int = 120,
    symbols: Sequence[str] | None = None,
    raw_columns: Sequence[str] = (),
) -> Panel: ...
```

Numeric features the panel must build: `activity_score`, `bias`, `price`,
`liquidity_fragility`, `confirmed`, `funding`, `oi_change_5m`, `oi_change_15m`,
`oi_change_1h`. Categorical: `move_type`. Plus, for each name in `raw_columns`, one
feature per (exchange, market) named `f"{exchange}_{market}_{column}"` taken from the
`market_metrics` row selected at that grid point.

`session_factory()` returns an async context manager with an async `execute`; see
`research/feed_health.py` for the existing usage pattern in this repo.

## Notes that will save you time

- **Missing is NaN, not zero.** Absent data must propagate as NaN so it can be excluded
  downstream. A zero would be read as a real observation of "no activity". `bias` is the
  one place a real 0.0 exists (`NONE`), so it is NaN only when there is no data at all.
- **Freshness is a lookback, not a nearest-neighbour match.** At grid time `t` a row
  counts if `0 <= t - row.timestamp <= tolerance`. Never use a row from the future: the
  panel must only see what the runtime could have seen at `t`.
- Derivative rows get their own, longer tolerance — they are polled far less often than
  books.
- Symbols come from what is actually present in the window, sorted. A `symbols` argument
  restricts that set; it does not add symbols with no data, and it does not reorder.
- Performance matters but correctness decides. A 24h window at 10s over 46 symbols is
  8,640 x 46; several minutes is acceptable, so prefer the clear implementation. Pulling
  each table once for the whole window and walking it in time order beats a query per
  grid point by a wide margin, and is also simpler.

## Acceptance

```acceptance
cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_research_fidelity.py -q
cd backend && ./.venv/Scripts/python.exe -m pytest -q
cd backend && ./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m ruff format --check .
```

All three must exit 0. The first currently fails at import with
`ModuleNotFoundError: No module named 'research.panel'`.

Report at the end:
1. The `pytest -q` summary line for the whole suite.
2. Roughly how long `test_every_cell_matches_the_reference_rebuild` takes.
3. Any test whose intent you found ambiguous, and how you resolved it.
4. Anything you found in `runtime.py` that the oracle's reference appears to get wrong.
   The reference is my work and it is not above being wrong; say so if you see it.
