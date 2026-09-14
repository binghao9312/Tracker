# Task T6: backend/research/evaluate.py — response curve, rank IC, barrier backtest

Create the module that `backend/tests/test_research_evaluate.py` imports. That test
already exists and currently fails with `ModuleNotFoundError`. It is the complete
specification: read it first and in full, including its module docstring, which names
the specific way each of the three functions is easy to get wrong.

These functions decide whether a candidate signal is worth trading. A wrong number here
does not crash — it silently justifies or kills a strategy.

## Hard constraints

- Create ONLY `backend/research/evaluate.py`.
- Do NOT modify anything under `backend/tests/`, and do not modify any other file. The
  tests are the oracle; editing them removes the only thing that can tell either of us
  the code is right. If you believe a test is wrong, stop and say so instead.
- `backend/app/` must never import from `backend/research/`.
- Reuse `research/forward.py` — `forward_returns`, `demean_by_symbol`, `block_stats`,
  `BlockStat`. Do NOT reimplement any of them. In particular do not compute your own
  standard error: `block_stats` exists because the naive one is wrong by a factor of
  tens on overlapping, cross-correlated data, and that mistake is invisible in output.
- No new third-party dependencies. `numpy` is available; `pandas` is NOT installed and
  must not be added. The spec that predates this task said these functions return a
  `DataFrame`; they return the dataclasses below instead.
- Do NOT run `uv`, `pip install`, or `poetry`, and do not create or modify lock files.

```scope
backend/research/evaluate.py
```

## Context

**Every shell command must be non-interactive or it will hang forever.** `git` needs
`--no-pager`.

Run everything from `C:\Users\HAO\Documents\Qtrade\backend` using this interpreter (the
bare `python` on PATH is a different install and lacks the dependencies):

```
./.venv/Scripts/python.exe
```

Read before writing anything:

- `backend/tests/test_research_evaluate.py` — the oracle.
- `backend/research/forward.py` — the statistics primitives, and their docstrings, which
  explain why each exists.
- `backend/research/panel.py` — the `Panel` you are given.

## Required interface

```python
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
    def describe(self) -> str: ...

def response_curve(
    panel, feature: str, horizon_seconds: int, *,
    buckets: int = 10, tradable_only: bool = True, demean: bool = True,
) -> ResponseCurve: ...

@dataclass(frozen=True)
class ICResult:
    feature: str
    horizon_seconds: int
    mean_ic: float          # NaN when no symbol qualified
    n_symbols: int
    positive_symbols: int
    n_observations: int
    per_symbol: Mapping[str, float]

def rank_ic(
    panel, feature: str, horizon_seconds: int, *,
    within_symbol: bool = True, absolute: bool = True, tradable_only: bool = True,
) -> ICResult: ...

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
    side: str            # "LONG" | "SHORT"
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    exit_reason: str     # "TAKE_PROFIT" | "STOP_LOSS" | "TIME_STOP"
    gross_bps: float
    net_bps: float

@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[Trade, ...]
    control: tuple[Trade, ...]
    stat: BlockStat          # net_bps of the signal trades
    control_stat: BlockStat  # net_bps of the control trades
    def describe(self) -> str: ...
```

## Semantics the tests depend on

**`rank_ic`.** Spearman rank correlation between the feature and the forward return.
`absolute=True` correlates against the magnitude of the return — the question is whether
the feature predicts how much the price moves, not which way. `within_symbol=True`
computes the correlation separately per symbol and averages; `False` pools every
observation into one correlation. These genuinely differ, and the difference is the
point: pooling lets the spread of volatility between symbols stand in for the
relationship inside each one. A symbol needs at least 3 paired observations to
contribute; symbols below that are excluded from `n_symbols` and from the mean.

**`response_curve`.** Bucket observations by feature value into `buckets` groups of
roughly equal count (quantile edges, not equal width — feature distributions here are
heavily skewed and equal-width buckets leave most of them empty), then report a
`BlockStat` of the forward return within each. Every finite observation lands in exactly
one bucket. `demean=True` applies `demean_by_symbol` first, which is what separates
timing from drift.

**`barrier_backtest`.** Walk the grid in time order.

- Enter when `activity_score >= entry_score`, `bias` is non-zero, the row is tradable,
  and fewer than `max_open_positions` are currently open. The cap is global across
  symbols, not per symbol. One open position per symbol at a time.
- Direction comes from `bias`: positive is LONG, negative is SHORT.
- After entry, scan forward. At each step test the stop **before** the target. When both
  are satisfied at the same step the stop wins; that tie-break is the one optimistic
  bias in the live engine that is reproducible at this sampling rate, and it is
  reversed here deliberately.
- Exit at `max_holding_seconds` with `TIME_STOP` if neither barrier is reached.
- `gross_bps` is the signed return of the position in bps — for a SHORT it is positive
  when the price fell. `net_bps = gross_bps - cost_bps`, charged once per round trip.
- For every signal trade, produce one control trade: same symbol, same side, same
  barriers, entry time drawn at random from that symbol's tradable rows. Seeded by
  `control_seed`, so the same seed gives the same controls and a different seed does
  not. This comparison is the single most effective guard against self-deception in the
  whole loop: without it a positive result cannot be told apart from the barrier
  geometry paying off on any entry whatsoever.
- Both groups' `BlockStat` must use the **same** block partition, so that the two
  numbers are comparable. `block_stats` already partitions on `time_index //
  block_size`; pass the entry index as the time index and the holding horizon in grid
  steps as the block size.

## Also required

Write the module docstring to record what this backtest **cannot** see, because the
number it produces will be quoted later by someone who did not write it:

- A price grid shows one price per step. A barrier touched and retraced between two
  samples is invisible, which biases results optimistically.
- Fills are assumed immediate and at the mid. There is no latency and no spread.

## Acceptance

```acceptance
cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_research_evaluate.py -q
cd backend && ./.venv/Scripts/python.exe -m pytest -q
cd backend && ./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m ruff format --check .
```

All three must exit 0. The first currently fails at import with
`ModuleNotFoundError: No module named 'research.evaluate'`.

Report at the end:
1. The `pytest -q` summary line for the whole suite.
2. Any test whose intent you found ambiguous, and how you resolved it.
3. Anything in the oracle that looks wrong to you. It is my work and it is not above
   being wrong; say so if you see it rather than working around it.
