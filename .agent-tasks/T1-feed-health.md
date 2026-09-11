# Task T1: backend/research/feed_health.py

Create a READ-ONLY diagnostic that measures per-feed write density in the metrics DB.

## Hard constraints
- Create ONLY these files: `backend/research/__init__.py`, `backend/research/feed_health.py`.
- Do NOT modify anything under `backend/app/` or `backend/tests/`.
- `backend/app/` must never import `backend/research/`.
- No new third-party dependencies. Standard library only (subprocess + json + statistics is fine).

## Context
Postgres runs in docker compose as service `postgres`, database `qtrade`, user `qtrade`.
It has NO host port mapping, so query it via:
    docker compose exec -T postgres psql -U qtrade -d qtrade -t -A -F'|' -c "<SQL>"
Run from the repo root (where docker-compose.yml lives).

Relevant tables and columns:
- market_metrics(timestamp timestamptz, exchange text, symbol text, market text, price, ...)
- flow_metrics(timestamp timestamptz, exchange, symbol, market, ...)
- derivative_metrics(timestamp timestamptz, exchange, symbol, ...)

`exchange` is 'binance' or 'okx'. `market` is 'spot' or 'perp'.
Some symbols are legacy junk with very few rows: only consider symbols having > 1000
rows in market_metrics.

## Required behaviour
`python -m research.feed_health` (run with cwd=backend) prints one table per source
table. For market_metrics and flow_metrics group by (exchange, market); for
derivative_metrics group by (exchange). Each row must show:

  exchange | market | symbols | rows | rows_per_symbol | gap_p50 | gap_p90 | gap_p99 | gap_max | gaps_over_60s

where gap_* are the seconds between consecutive rows for the same
(exchange, market, symbol), and gaps_over_60s counts gaps > 60 seconds.

Compute the gap percentiles in SQL (percentile_cont over a window/lag), not by
pulling millions of rows into Python.

Support `--hours N` (default 6) to restrict to the last N hours, and
`--symbol SYM` (repeatable) to restrict to specific symbols.

Print a final WARNINGS section listing any (exchange, market) whose gap_p50 is more
than 2x the median gap_p50 across all feeds, formatted as:
  WARNING: binance/perp gap_p50=15.50s is 5.7x the cross-feed median (2.71s)

## Acceptance
From the repo root, both of these must succeed and produce the tables:
    cd backend && python -m research.feed_health --hours 6
    cd backend && python -m research.feed_health --hours 48 --symbol BTCUSDT --symbol SOLUSDT
And these must both pass unchanged:
    python -m pytest        (run from backend/)
    ruff check . && ruff format --check .   (run from backend/)

Report at the end: the actual table output for `--hours 48`, and whether binance/perp
is flagged in WARNINGS.
