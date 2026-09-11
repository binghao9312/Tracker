# T6 — Replay harness (backtest) driving the real paper engine

Repo root is the current directory. Backend lives in `backend/`.

**The tests already exist and already fail: `backend/tests/test_replay.py`. They are
the specification -- read them first, they are more precise than this prose.** Your
job is to write `backend/research/replay.py` so they pass.

This is a backtest. Its failure mode is not a crash, it is a confident wrong number
that someone then trades on. Every rule below exists because breaking it produces
plausible-looking output.

## Hard constraints
- Create ONLY: `backend/research/replay.py`
- Modify ONLY: `backend/research/__init__.py` (if an export is needed), `README.md`
- **Do NOT modify ANY file under `backend/tests/`** -- not `test_replay.py`, not any
  helper, not any existing test. That is the entire point of this task.
- Do NOT modify anything under `backend/app/`. In particular do NOT change
  `paper_trading.py`: the harness must USE `PaperTradingEngine`, not adjust it.
- No new third-party dependencies. `numpy` is available via the `research` extra if
  you want it, but it is not required and plain Python is fine.

## Context the task depends on
- Run everything from `backend/`. The interpreter is `.venv/Scripts/python`
  (Windows layout under git-bash): `cd backend && ./.venv/Scripts/python -m pytest`.
  Do not create a venv and do not install anything.
- `backend/research/forward.py` shows the house style for this package (dataclasses,
  explicit units, no hidden global state). Follow it.
- `PaperTradingEngine` lives in `backend/app/paper_trading.py`. Drive it through its
  public `process_update(symbol, detail, now)` and give it an in-memory repository --
  `backend/tests/paper_helpers.py::MemoryPaperRepository` shows the exact interface a
  repository must satisfy (`open_trade`, `close_trade`, `get_open_positions`,
  `get_recent_closed_positions`, `append_event`). Your harness needs its own
  implementation of that interface inside `research/replay.py`; do NOT import from
  `tests/`.
- The `detail` mapping the engine consumes is built by
  `LiveRuntime._build_detail` in `backend/app/runtime.py` (the `detail = {...}`
  literal, around line 610). Your job is to rebuild a `detail` of the SAME SHAPE from
  persisted rows. The keys that matter for entry and exit are: `price`,
  `activity_score`, `liquidity_fragility`, `move_type`, `cross_exchange_state`,
  `oi_change_5m`, `funding`, `spot`, `perp`, and `orderbooks`.
- `orderbooks` is shaped `{exchange: {market: {"exchange":..., "market":...,
  "bids": [[price, qty], ...], "asks": [...]}}}`. `_execution_book` in
  `paper_trading.py` is what reads it; make sure what you build satisfies it.
- Entry bias comes from `calculate_trade_signal` in `backend/app/trade_signal.py`,
  which reads `detail["spot"]` and `detail["perp"]` pressure/volume values. Those map
  to the persisted `flow_metrics` columns.

## Units -- get these wrong and every number downstream is wrong
From `backend/app/liquidity.py`:
- `spread_percent = (best_ask - best_bid) / mid * 100`. So a synthesized book puts
  each side HALF a spread away from the mid:
  `bid = mid * (1 - spread_percent / 200)`, `ask = mid * (1 + spread_percent / 200)`.
- `bid_depth_2` / `ask_depth_2` are **quote notional in USDT**, accumulated over the
  levels within 2% of the mid -- NOT base quantity. A synthesized book level needs
  base quantity, so divide by that level's price.

## Required behaviour
1. `VenueQuote` and `Snapshot` frozen dataclasses with exactly the fields the tests
   construct (see `test_replay.py`: `snapshot()` and `BookSynthesisTests`).
   `Snapshot.spot` and `Snapshot.perp` are plain mappings of flow values; `venues` is
   a tuple of `VenueQuote`.
2. `synthesize_book(quote) -> dict` producing one bid level and one ask level per the
   units section, including the `exchange` and `market` keys.
3. `async def replay(snapshots, settings, *, max_gap_seconds: float = 5.0) -> ReplayResult`:
   - Feeds each snapshot to a single `PaperTradingEngine` in timestamp order,
     grouping by symbol so symbols are independent of each other.
   - **Raises `ValueError` on non-monotonic timestamps.** Do not sort the input to
     paper over it: descending DB order is a real mistake and it must fail loudly
     rather than silently producing an inverted backtest.
   - **A gap larger than `max_gap_seconds` between consecutive snapshots invalidates
     accumulated signal persistence**, because nothing was observed across it. Reset
     the engine's per-symbol arming/persistence state for that symbol (construct a
     fresh engine per symbol, or reset the state it exposes) and count the gap.
     Without this, two rows ten minutes apart look like a signal that held.
   - Returns a `ReplayResult` with at least: `trades` (closed and open trade rows as
     the repository holds them), `events` (every event the engine emitted, each with
     its `reason`), `stats`, `fill_model`, `cadences` (number of snapshots consumed),
     and `gaps`.
4. `stats` must include `total`, `win_rate`, `profit_factor`, `average_return`,
   `by_exit_reason` (a dict of reason -> count), and `fill_model`. `win_rate` counts a
   trade as a win on positive `net_pnl`/`return_pct`; state which in a docstring and
   be consistent.
5. `fill_model` is a short human-readable string stating the approximation, e.g.
   `"synthesized single-level book from persisted mid/spread/depth_2"`. It must appear
   in `stats` as well, so that anyone reading the numbers sees the caveat attached to
   them. Do NOT invent liquidity beyond the recorded depth: if the configured
   notional cannot be filled, let the engine's own `INSUFFICIENT_BOOK_DEPTH` path
   happen and record the event.
6. A DB-backed loader: `async def load_snapshots(session_factory, symbol, start, end,
   *, limit=3600) -> list[Snapshot]` that joins `signal_metrics`, `market_metrics` and
   `flow_metrics` on `(symbol, timestamp)` and returns snapshots ascending. The
   `signal_metrics` table and `MetricRepository.signal_history` were added in the
   commit before this one -- read `backend/app/database.py` and
   `backend/app/repository.py` for the actual column names rather than guessing.
   Rows with no `signal_metrics` entry are skipped, not defaulted to zero.
7. A `python -m research.replay --symbol BTCUSDT --hours 6` CLI entry point that
   prints the stats table and, prominently, the `fill_model` line. Follow the CLI
   shape of `backend/research/feed_health.py`.

## Explicitly out of scope
Do not try to reconstruct `liquidity_fragility` or `activity_score` from raw inputs.
They are persisted now; read them. If a snapshot has `activity_score is None`, skip
that cadence rather than substituting a value -- a substituted score fabricates an
entry condition.

## Acceptance
Run from the repo root and report the real output of each:

    cd backend && ./.venv/Scripts/python -m pytest
    cd backend && ./.venv/Scripts/python -m ruff check .
    cd backend && ./.venv/Scripts/python -m ruff format --check .
    cd backend && ./.venv/Scripts/python -m mypy app research

All four must pass, with **every test in `tests/test_replay.py` passing** and zero
failures anywhere. `git diff --name-only` plus `git status --short` must show only
the files this spec allows -- if anything under `backend/tests/` appears, the task has
failed regardless of the test count.

Do not run the CLI against the live database: the `signal_metrics` table is empty
until the backend has run with the new persistence, so an empty result there is
expected and is not a reason to change anything.

In your final report paste: the pytest summary line, the mypy line,
`git status --short`, and the `fill_model` string you chose.
