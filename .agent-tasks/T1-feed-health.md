# Task T1: backend/research/feed_health.py

Create a read-only diagnostic that measures per-feed write density in the metrics
database, so we can find out why one exchange feed is far sparser than the others.

## Hard constraints

- Create ONLY these two files:
  - `backend/research/__init__.py`
  - `backend/research/feed_health.py`
- Do NOT modify anything under `backend/app/` or `backend/tests/`
- Do NOT add, install, or lock any dependency. Standard library only.
  Specifically: do NOT run `uv`, `pip install`, `poetry`, or create `uv.lock`.
  Use the interpreter that is already on PATH.
- `backend/app/` must never import from `backend/research/`

```scope
backend/research/**
```

## Context

**Every shell command you run must be non-interactive or it will hang forever.**
Notably `docker compose exec` needs `-T`, and `git` commands need `--no-pager`.

Postgres runs as docker compose service `postgres`, database `qtrade`, user `qtrade`.
There is NO host port mapping, so the only way to query it is:

```
docker compose exec -T postgres psql -U qtrade -d qtrade -t -A -F'|' -c "<SQL>"
```

Run that from the repo root (where `docker-compose.yml` is). `-t -A -F'|'` gives you
tuples-only, unaligned, pipe-separated output that is easy to parse.

Tables and the columns that matter:

- `market_metrics(timestamp timestamptz, exchange text, symbol text, market text, ...)`
- `flow_metrics(timestamp timestamptz, exchange text, symbol text, market text, ...)`
- `derivative_metrics(timestamp timestamptz, exchange text, symbol text, ...)`
  — note: derivative_metrics has NO `market` column.

`exchange` is `'binance'` or `'okx'`. `market` is `'spot'` or `'perp'`.

The database contains legacy junk symbols from earlier universe configurations. Filter
them out by only considering symbols with more than 1000 rows in `market_metrics`.

## Required behaviour

`python -m research.feed_health` — run with the working directory set to `backend/` —
prints one table per source table. Group `market_metrics` and `flow_metrics` by
`(exchange, market)`; group `derivative_metrics` by `(exchange)` alone.

Each row shows:

```
exchange | market | symbols | rows | rows_per_symbol | gap_p50 | gap_p90 | gap_p99 | gap_max | gaps_over_60s
```

`gap_*` are seconds between consecutive rows for the same
`(exchange, market, symbol)` series. `gaps_over_60s` counts gaps longer than 60s.

Compute the gap percentiles **in SQL** using `lag()` over a window plus
`percentile_cont`. Do not pull millions of rows into Python.

Options:
- `--hours N` — restrict to the last N hours (default 6)
- `--symbol SYM` — restrict to specific symbols, repeatable

End with a WARNINGS section listing any `(exchange, market)` whose `gap_p50` exceeds
2x the median `gap_p50` across all feeds:

```
WARNING: binance/perp gap_p50=15.50s is 5.7x the cross-feed median (2.71s)
```

If nothing exceeds the threshold, print `WARNINGS: none`.

## Acceptance

```acceptance
cd backend && python -m research.feed_health --hours 6
cd backend && python -m research.feed_health --hours 48 --symbol BTCUSDT --symbol SOLUSDT
cd backend && python -m pytest
cd backend && ruff check . && ruff format --check .
```

Report at the end: the full table output for `--hours 48`, and state explicitly
whether `binance/perp` appears in WARNINGS and at what multiple.
