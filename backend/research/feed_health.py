"""Diagnostic measuring per-feed write density in the metrics database."""

from __future__ import annotations

import argparse
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FeedMetrics:
    exchange: str
    market: str
    symbols: int
    rows: int
    rows_per_symbol: float | None
    gap_p50: float | None
    gap_p90: float | None
    gap_p99: float | None
    gap_max: float | None
    gaps_over_60s: int


def find_repo_root() -> Path:
    """Locate the repo root containing docker-compose.yml."""
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        if (parent / "docker-compose.yml").is_file():
            return parent
    return Path.cwd()


def run_psql_query(sql: str, repo_root: Path) -> str:
    """Execute SQL query via docker compose against the postgres service."""
    cmd = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "qtrade",
        "-d",
        "qtrade",
        "-t",
        "-A",
        "-F|",
        "-c",
        sql,
    ]
    res = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


def build_market_or_flow_query(table: str, hours: float, symbols: list[str]) -> str:
    """Build SQL query for market_metrics or flow_metrics."""
    sym_clause = ""
    if symbols:
        escaped = ", ".join(f"'{s}'" for s in symbols)
        sym_clause = f"AND symbol IN ({escaped})"

    return f"""
WITH max_t AS (
    SELECT COALESCE(MAX(timestamp), NOW()) AS max_ts FROM {table}
),
valid_symbols AS (
    SELECT symbol FROM market_metrics GROUP BY symbol HAVING count(*) > 1000
),
lagged AS (
    SELECT
        exchange,
        market,
        symbol,
        timestamp,
        EXTRACT(EPOCH FROM (timestamp - LAG(timestamp) OVER (
            PARTITION BY exchange, market, symbol ORDER BY timestamp
        ))) AS gap
    FROM {table}, max_t
    WHERE symbol IN (SELECT symbol FROM valid_symbols)
      {sym_clause}
      AND timestamp >= max_t.max_ts - interval '{hours} hours'
)
SELECT
    exchange,
    market,
    count(DISTINCT symbol) AS symbols,
    count(*) AS rows,
    round(count(*)::numeric / NULLIF(count(DISTINCT symbol), 0), 2) AS rows_per_symbol,
    round(percentile_cont(0.50) WITHIN GROUP (ORDER BY gap)::numeric, 2) AS gap_p50,
    round(percentile_cont(0.90) WITHIN GROUP (ORDER BY gap)::numeric, 2) AS gap_p90,
    round(percentile_cont(0.99) WITHIN GROUP (ORDER BY gap)::numeric, 2) AS gap_p99,
    round(max(gap)::numeric, 2) AS gap_max,
    count(*) FILTER (WHERE gap > 60) AS gaps_over_60s
FROM lagged
GROUP BY exchange, market
ORDER BY exchange, market;
"""


def build_derivative_query(hours: float, symbols: list[str]) -> str:
    """Build SQL query for derivative_metrics grouped by exchange."""
    sym_clause = ""
    if symbols:
        escaped = ", ".join(f"'{s}'" for s in symbols)
        sym_clause = f"AND symbol IN ({escaped})"

    return f"""
WITH max_t AS (
    SELECT COALESCE(MAX(timestamp), NOW()) AS max_ts FROM derivative_metrics
),
valid_symbols AS (
    SELECT symbol FROM market_metrics GROUP BY symbol HAVING count(*) > 1000
),
lagged AS (
    SELECT
        exchange,
        symbol,
        timestamp,
        EXTRACT(EPOCH FROM (timestamp - LAG(timestamp) OVER (
            PARTITION BY exchange, symbol ORDER BY timestamp
        ))) AS gap
    FROM derivative_metrics, max_t
    WHERE symbol IN (SELECT symbol FROM valid_symbols)
      {sym_clause}
      AND timestamp >= max_t.max_ts - interval '{hours} hours'
)
SELECT
    exchange,
    'perp' AS market,
    count(DISTINCT symbol) AS symbols,
    count(*) AS rows,
    round(count(*)::numeric / NULLIF(count(DISTINCT symbol), 0), 2) AS rows_per_symbol,
    round(percentile_cont(0.50) WITHIN GROUP (ORDER BY gap)::numeric, 2) AS gap_p50,
    round(percentile_cont(0.90) WITHIN GROUP (ORDER BY gap)::numeric, 2) AS gap_p90,
    round(percentile_cont(0.99) WITHIN GROUP (ORDER BY gap)::numeric, 2) AS gap_p99,
    round(max(gap)::numeric, 2) AS gap_max,
    count(*) FILTER (WHERE gap > 60) AS gaps_over_60s
FROM lagged
GROUP BY exchange
ORDER BY exchange;
"""


def parse_query_output(output: str) -> list[FeedMetrics]:
    """Parse pipe-separated psql output into FeedMetrics records."""
    records: list[FeedMetrics] = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 10:
            continue
        records.append(
            FeedMetrics(
                exchange=parts[0],
                market=parts[1],
                symbols=int(parts[2]) if parts[2] else 0,
                rows=int(parts[3]) if parts[3] else 0,
                rows_per_symbol=float(parts[4]) if parts[4] else None,
                gap_p50=float(parts[5]) if parts[5] else None,
                gap_p90=float(parts[6]) if parts[6] else None,
                gap_p99=float(parts[7]) if parts[7] else None,
                gap_max=float(parts[8]) if parts[8] else None,
                gaps_over_60s=int(parts[9]) if parts[9] else 0,
            )
        )
    return records


def print_table(name: str, records: list[FeedMetrics]) -> None:
    """Print formatted table for a source table."""
    print(f"=== {name} ===")
    print(
        "exchange | market | symbols | rows | rows_per_symbol | "
        "gap_p50 | gap_p90 | gap_p99 | gap_max | gaps_over_60s"
    )
    for r in records:
        rps_str = f"{r.rows_per_symbol:.2f}" if r.rows_per_symbol is not None else "N/A"
        p50_str = f"{r.gap_p50:.2f}" if r.gap_p50 is not None else "N/A"
        p90_str = f"{r.gap_p90:.2f}" if r.gap_p90 is not None else "N/A"
        p99_str = f"{r.gap_p99:.2f}" if r.gap_p99 is not None else "N/A"
        max_str = f"{r.gap_max:.2f}" if r.gap_max is not None else "N/A"
        print(
            f"{r.exchange} | {r.market} | {r.symbols} | {r.rows} | {rps_str} | "
            f"{p50_str} | {p90_str} | {p99_str} | {max_str} | {r.gaps_over_60s}"
        )
    print()


def print_warnings(records: list[FeedMetrics]) -> None:
    """Print WARNINGS section for feeds with gap_p50 > 2x cross-feed median."""
    feed_records = [r for r in records if r.market in ("spot", "perp")]
    p50_values = [r.gap_p50 for r in feed_records if r.gap_p50 is not None]
    median_p50 = statistics.median(p50_values) if p50_values else 0.0

    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()

    for r in feed_records:
        if r.gap_p50 is not None and median_p50 > 0.0:
            if r.gap_p50 > 2.0 * median_p50:
                feed_key = (r.exchange, r.market)
                if feed_key not in seen:
                    seen.add(feed_key)
                    ratio = r.gap_p50 / median_p50
                    warnings.append(
                        f"WARNING: {r.exchange}/{r.market} gap_p50={r.gap_p50:.2f}s "
                        f"is {ratio:.1f}x the cross-feed median ({median_p50:.2f}s)"
                    )

    if warnings:
        for w in warnings:
            print(w)
    else:
        print("WARNINGS: none")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Feed health write density diagnostic.")
    parser.add_argument(
        "--hours",
        type=float,
        default=6.0,
        help="Restrict to the last N hours (default: 6)",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        default=[],
        help="Restrict to specific symbols (repeatable)",
    )

    args = parser.parse_args(argv)

    # Sanitize symbol inputs
    clean_symbols: list[str] = []
    if args.symbols:
        for sym in args.symbols:
            s = sym.strip().upper()
            if re.match(r"^[A-Z0-9_]+$", s):
                clean_symbols.append(s)

    repo_root = find_repo_root()

    tables = [
        ("market_metrics", build_market_or_flow_query("market_metrics", args.hours, clean_symbols)),
        ("flow_metrics", build_market_or_flow_query("flow_metrics", args.hours, clean_symbols)),
        ("derivative_metrics", build_derivative_query(args.hours, clean_symbols)),
    ]

    all_records: list[FeedMetrics] = []

    for name, sql in tables:
        try:
            output = run_psql_query(sql, repo_root)
            records = parse_query_output(output)
        except subprocess.CalledProcessError as e:
            sys.stderr.write(f"Error querying {name}: {e.stderr}\n")
            records = []
        print_table(name, records)
        if name == "market_metrics":
            all_records = records

    print_warnings(all_records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
