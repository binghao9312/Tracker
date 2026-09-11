# T5 — Persist the symbol-level signal state, with Alembic

Repo root is the current directory. Backend lives in `backend/`.

Right now `activity_score`, `liquidity_fragility`, `move_type` and
`cross_exchange_state` exist only in memory and in the WebSocket payload. They are
the variables that decide whether a trade is entered, and none of them is stored --
so 3.2M rows of history cannot tell you whether an entry should have fired. This
task fixes that, and introduces Alembic so the schema change is deployable.

## Hard constraints
- Modify ONLY: `backend/app/database.py`, `backend/app/repository.py`,
  `backend/app/runtime.py`, `backend/pyproject.toml`, `README.md`
- Create ONLY: `backend/alembic.ini`, `backend/migrations/**` (Alembic scaffolding
  and versions), `backend/tests/test_signal_persistence.py`
- Do NOT modify any existing test file, and do NOT modify
  `backend/app/paper_trading.py`, `backend/app/api.py`, `backend/app/scoring.py`,
  `config/`, or `frontend/`
- The ONLY new dependency permitted is `alembic>=1.14,<2` (already installed in the
  venv). Add it to `[project.optional-dependencies] dev`, NOT to the runtime
  `dependencies` list -- migrations are an operator tool, not a runtime import.

## Context the task depends on
- Run everything from `backend/`. The interpreter is `.venv/Scripts/python`
  (Windows layout under git-bash): `cd backend && ./.venv/Scripts/python -m pytest`.
  Do not create a venv and do not install anything; alembic is already present.
- Baseline: **148 passed**, `ruff check`, `ruff format --check`, `mypy app` all clean.
  Keep it that way.
- A live PostgreSQL is running in Docker for manual verification only:
  `docker exec qtrade-postgres-1 psql -U qtrade -d qtrade -c "..."`. It already holds
  ~3.2M `market_metrics`, ~3.2M `flow_metrics`, ~770k `derivative_metrics` rows and
  **0** `paper_trades`. Do NOT delete or rewrite existing rows.
- `backend/app/database.py:153` currently creates the schema with
  `Base.metadata.create_all`. Existing tables must keep working; see "Required
  behaviour" item 4 for exactly how the two mechanisms must coexist.
- The values to persist are built in `LiveRuntime._build_detail`
  (`backend/app/runtime.py`, the `detail = {...}` literal around line 610), and
  `flush()` (around line 230) is where the per-cadence batches are assembled and
  handed to `_persist_metrics_batch`.
- `MarketMetricRow` / `FlowMetricRow` / `DerivativeMetricRow` in
  `backend/app/database.py` are the model pattern to follow, and
  `MetricRepository.persist_*` in `backend/app/repository.py` is the write pattern.

## The grain decision -- already made, do not redesign it
These values are **per symbol**, not per exchange/market. Do NOT add them as columns
on `market_metrics`: that table has one row per (exchange, market, symbol) per
second, so symbol-level values would be duplicated four times per second and invite
exactly the mixed-venue confusion that already exists elsewhere.

Create a NEW table at the correct grain:

    signal_metrics
      id                     integer primary key
      timestamp              timestamptz  not null
      symbol                 varchar(32)  not null
      price                  double precision  null   -- the aggregate preferred mid
      activity_score         double precision  null
      liquidity_fragility    double precision  null
      move_type              varchar(24)  null
      cross_exchange_state   varchar(24)  null
      oi_change_5m           double precision  null
      oi_change_15m          double precision  null
      oi_change_1h           double precision  null
      funding_rate           double precision  null

Nullable is deliberate: a symbol can legitimately have no score yet, and a NULL must
stay distinguishable from a 0.0 score. Add an index on `(symbol, timestamp)` --
replay queries read one symbol over a time range.

## Required behaviour
1. `SignalMetricRow` model in `database.py` matching the table above.
2. `MetricRepository.persist_signals(rows)` following the existing bulk-insert
   pattern, plus `signal_history(symbol, start_time, end_time, limit)` returning rows
   ascending by timestamp. Give the read a **mandatory** limit with a sane default
   (say 3600) -- an unbounded range query over this table is a footgun.
3. `flush()` collects one signal row per active symbol per cadence from the `detail`
   it already builds, and persists them in the same batch call as the other metrics.
   Reuse the existing timestamp convention. Do not add a second DB round trip per
   symbol -- it must be one bulk write per cadence, like the other three.
4. Alembic:
   - `alembic.ini` + `migrations/` scaffolding under `backend/`, with `env.py` wired
     to `Base.metadata` and reading the URL from the same env var the app uses
     (see `backend/app/database.py`). It must work with the async driver
     (`postgresql+asyncpg`) -- use the Alembic async template / `run_async_migrations`.
   - ONE initial migration that stamps the EXISTING schema (all five current tables)
     as the baseline, and a SECOND migration that adds `signal_metrics`.
     Do not write a migration that would drop or recreate existing tables.
   - `create_all` must remain for tests/local bootstrap, and must stay consistent
     with the migrations -- i.e. a fresh DB built by `alembic upgrade head` and one
     built by `create_all` must end up with the same tables.
5. README: a short "Database migrations" section with the two commands (autogenerate
   and upgrade) and a note that `create_all` still covers the test/dev path.

## Required tests (`backend/tests/test_signal_persistence.py`)
Follow the style of the existing tests in `backend/tests/` (look at how the current
repository tests fake or drive the session; do not add pytest plugins). Cover:
- A signal row round-trips: persist, then `signal_history` returns it with every
  field intact, ascending by timestamp.
- A NULL `activity_score` comes back as `None`, not `0.0` (this is the regression
  that matters -- a coerced default would silently fake entry conditions).
- `signal_history` honours its limit and its symbol filter, and never returns another
  symbol's rows.
- `flush()` emits exactly one signal row per active symbol per cadence, carrying the
  same `activity_score` that the published detail carries -- drive the real
  `LiveRuntime.flush()` the way `test_runtime_pipeline.py` already does, rather than
  asserting on a hand-built dict.

## Acceptance
Run from the repo root and report the real output of each:

    cd backend && ./.venv/Scripts/python -m pytest
    cd backend && ./.venv/Scripts/python -m ruff check .
    cd backend && ./.venv/Scripts/python -m ruff format --check .
    cd backend && ./.venv/Scripts/python -m mypy app
    cd backend && ./.venv/Scripts/python -m alembic upgrade head
    docker exec qtrade-postgres-1 psql -U qtrade -d qtrade -c "\d signal_metrics"
    docker exec qtrade-postgres-1 psql -U qtrade -d qtrade -c "select count(*) from market_metrics;"

The first four must pass with MORE passing tests than the 148 baseline and zero
failures. `alembic upgrade head` must succeed against the live database, and the
`market_metrics` count must still be in the millions afterwards -- if a migration
truncates or recreates an existing table, that is a hard failure of this task.

In your final report paste: the pytest summary line, the mypy line, the
`\d signal_metrics` output, the `market_metrics` count, and `git status --short`.
