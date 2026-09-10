# Tracker Live Soak Test & Data Sanity Audit Report

## 1. Commit Tested

```text
SHA: 764ba294606a400489f36e3d666f872961924e88
Start time: 2026-09-09T20:37:17+08:00
End time: 2026-09-09T21:50:16+08:00
Runtime duration: 1h 12m 59s
```

Working tree was clean. Startup used `docker compose up --build`. Backend, frontend, and PostgreSQL started; PostgreSQL remained healthy. The scanner discovered 44 active symbols. The UI loaded at `http://localhost:5174` with live scanner data.

## 2. CI / Static Verification

```text
pytest: PASS — 89 passed, 1 dependency deprecation warning
ruff: PASS — All checks passed
frontend build: PASS — TypeScript and Vite production build completed
GitHub CI: PASS — run 15 for the tested SHA
```

GitHub evidence: [CI run 34348299692](https://github.com/binghao9312/Tracker/actions/runs/34348299692)

## 3. Exchange Stream Health

| Exchange | Market | Trade | Depth | Reconnects | Errors | Status |
|---|---|---|---|---:|---|---|
| Binance | Spot | Live; BTC/ETH/SOL and later AR flow changed continuously | Intermittent; substantially less fresh than OKX | Trade 0; depth 76 resyncs | 18 sequence gaps, 3 HTTP 429, 55 HTTP 418 | **FAIL** |
| Binance | Perp | Live and non-zero for BTC/ETH/SOL | Frozen for the selected majors throughout the soak | Trade 0; depth 173 resyncs | 12 sequence gaps, 6 HTTP 429, 155 HTTP 418 | **FAIL** |
| OKX | Spot | Live | Continuously updating | Trade 0; depth 0 | None observed | PASS |
| OKX | Perp | Live | Continuously updating; independent recovery observed | Trade 0; depth 1 | One WebSocket disconnect; recovered | PASS |

Additional runtime evidence:

- Binance derivative polling: **183 retries**—108 HTTP 429 and 75 HTTP 418.
- No trade reconnect logs.
- No traceback, unhandled exception, JSON decode failure, or unhandled task exception.
- Backend stayed alive for the full soak.
- Binance perpetual depth did not recover:
  - BTC: 1 distinct price and 2 OBI values across 1,713 persisted rows.
  - ETH: 1 distinct price and 1 OBI value across 1,713 rows.
  - SOL: 1 distinct price and 2 OBI values across 1,713 rows.
- In comparison, OKX perpetual OBI changed in 1,709 of 1,710 rows for each major.

## 4. Selected Symbol Sanity

ARUSDT was selected as the lower-liquidity asset. It was market-cap rank 47 and had live OKX Spot/Perp coverage; Binance Spot also became available later.

Snapshot near the end of the pre-restart soak:

| Symbol | Price | Spot CVD 1m / 5m | Perp CVD 1m / 5m | Spot pressure B/S 1m; 5m | Perp pressure B/S 1m; 5m | OI 5m / 15m / 1h | Funding | Fragility / Activity | Move / Cross-exchange |
|---|---:|---:|---:|---|---|---|---:|---|---|
| BTCUSDT | 79,427.95 | -144,843 / -3,030,722 | 4,977,286 / -17,166,091 | 0.147/0.102; 0.841/0.920 | 1.132/0.502; 7.270/7.210 | -0.002017 / 0.000204 / -0.005657 | 0.00005923 | 0 / 29.70 | NEUTRAL / DIVERGENT |
| ETHUSDT | 2,504.665 | 260,774 / -1,815,855 | 6,137,182 / -14,644,785 | 0.122/0.044; 0.729/0.986 | 0.618/0.326; 4.765/6.633 | -0.003286 / -0.004131 / -0.009012 | 0.00003170 | 0 / 31.10 | NEUTRAL / CONFIRMED |
| SOLUSDT | 104.405 | 73,557 / -765,216 | -115,143 / -6,189,975 | 0.025/0.011; 0.088/0.135 | 0.038/0.031; 0.286/0.334 | -0.002302 / -0.004016 / -0.014965 | -0.00001814 | 0 / 11.08 | NEUTRAL / DIVERGENT |
| ARUSDT | 2.809 | 4,936 / -1,694 | 465 / -4,655 | 0.093/0.012; 0.158/0.153 | 0.012/0.001; 0.286/0.237 | -0.000323 / 0.000451 / 0.005776 | 0.00010000 | 90 / 6.29 | NEUTRAL / DIVERGENT |

Trade-flow checks:

- Spot and Perp values were not duplicates.
- BTC, ETH, and SOL Binance Perp flow remained non-zero and changed continuously.
- CVD 1m and 5m diverged after startup and changed independently as trades aged out.
- DB evidence showed 1,700+ distinct 1m/5m CVD values for major Binance Perp streams.
- Early equality was limited to approximately the first 45–49 persisted rows after cold startup.
- No NaN or Infinity was found across all 44 live symbol metric payloads.
- Pressure/CVD values were finite. However, pressures derived from stale Binance Perp depth are not trustworthy.

## 5. Order Book Sanity

Representative perpetual comparison at `2026-09-09T12:42:56Z`:

| Symbol | Exchange | Bid + Ask depth ±1% | $10k buy/sell impact | $50k buy/sell impact |
|---|---|---:|---:|---:|
| BTCUSDT | Binance | $7.34M | +0.00000063% / -0.00000063% | +0.00000063% / -0.00000063% |
| BTCUSDT | OKX | $12.44M | +0.00000063% / -0.00000063% | +0.00000063% / -0.00000063% |
| ETHUSDT | Binance | $12.32M | +0.00000200% / -0.00000200% | +0.00000200% / -0.00000200% |
| ETHUSDT | OKX | $14.66M | +0.00000199% / -0.00000199% | +0.00000199% / -0.00000199% |
| SOLUSDT | Binance | $36.33M | +0.00004789% / -0.00004789% | +0.00004789% / -0.00004789% |
| SOLUSDT | OKX | $18.49M | +0.00004780% / -0.00004780% | +0.00004780% / -0.00004780% |
| ARUSDT | OKX | $80.87K | +0.001958% / -0.001150% | +0.008973% / -0.005121% |

No obvious 10×/100×/1000× OKX perpetual quantity-normalization error was observed. BTC, ETH, and SOL exchange depth differed by approximately 1.2×–2.0×.

Final aggregate liquidity examples:

- SOL capital to move 1%: $18.52M up / $18.03M down.
- AR capital to move 1%: $35.09K up / $47.58K down.
- AR capital to move 2%: $48.17K up / $77.17K down.
- BTC/ETH capital-to-move values were null because the retained top-200 depth did not reach the requested percentage threshold.
- Price, spread, depth, capital-to-move, and OBI sanity constraints passed wherever values were present.
- Sell impacts are intentionally signed negative values.

The magnitude comparison itself looked plausible, but **Binance Perp books were frozen after their initial snapshots**. Freshness failure makes the later Binance values unsuitable for signals or historical analysis.

## 6. OI Window Validation

```text
5m: FAIL overall — OKX/global PASS; Binance stale/unavailable
15m: FAIL overall — OKX/global PASS; Binance stale/unavailable
1h: FAIL overall — OKX/global PASS after one hour; Binance stale/unavailable
```

Observed behavior:

- Early 5m/15m/1h values were correctly null.
- OKX/global 5m populated after sufficient history.
- OKX/global 15m populated after sufficient history.
- OKX/global 1h populated after approximately one hour.
- Signed negative values remained negative.
- Missing Binance values did not become zero.
- Global OI used the available OKX move, including:
  - BTC 1h: approximately `-0.0057`
  - ETH 1h: approximately `-0.0094`
  - SOL 1h: approximately `-0.0150`
- Per-exchange API fields remained visible.

Binance failure evidence:

- BTC and ETH each had only **one distinct Binance OI value** during the entire soak.
- SOL had no current Binance derivative series.
- Despite that, the database accumulated new Binance derivative rows every cadence with advancing timestamps.
- Binance 5m/15m/1h remained null while OKX accumulated hundreds of distinct OI observations.

## 7. Activity Score Distribution

Final 44-symbol distribution:

```text
min:     2.70
median:  9.90
p75:    13.8575
p90:    22.3090
p95:    25.4210
max:    31.10
>=60:    0
>=70:    0
>=80:    0
>=90:    0
```

Across 95 samples:

- Median range: `6.925` to `13.505`.
- Highest observed score: `52.75` on BTCUSDT.
- No score reached 60, 70, 80, or 90.
- Scores were distributed rather than identical.
- No pathological “all zero” or “most above 80” distribution appeared.

Thresholds were not changed. This distribution cannot independently clear the system because part of the liquidity input was stale.

## 8. Database Persistence

Two measurements separated by 68 minutes:

| Table | T0 rows — 12:38:46Z | T1 rows — 13:46:46Z | Increase |
|---|---:|---:|---:|
| `market_metrics` | 518,601 | 745,269 | +226,668 |
| `flow_metrics` | 480,598 | 707,229 | +226,631 |
| `derivative_metrics` | 182,480 | 276,302 | +93,822 |
| `paper_trades` | 0 | 0 | 0 |
| `paper_trade_events` | 0 | 0 | 0 |

Recent timestamps advanced continuously.

Post-restart counts at `13:48:53Z`:

```text
market:     749,071
flow:       710,712
derivative: 277,624
```

Persistence restart result: **PASS**.

- Backend was restarted after the soak.
- Scanner recovered with 44 symbols.
- `/api/symbol/BTCUSDT/history` still returned 3,600 records per metric category.
- Timestamp overlap with pre-restart history:
  - Market: 3,545/3,600
  - Flow: 3,545/3,600
  - Derivative: 3,575/3,600
- New timestamps advanced after restart.

Retention verification:

- Runtime config loaded `data_retention.metric_history_days: 30`.
- Cutoff is calculated as current UTC time minus `30 × 24` hours.
- Pruning runs every 3,600 one-second cadence cycles.
- Only market, flow, and derivative tables are pruned.
- Paper trades and events are excluded.

Growth concern:

- Observed rates: approximately 55.6 market, 55.5 flow, and 23.0 derivative rows/second.
- Table sizes at T1: 202 MB market, 163 MB flow, 52 MB derivative.
- Current row-size/rate extrapolation: approximately **2.92 GB/day**, or **87.7 GB over 30 days**, before additional PostgreSQL/WAL overhead.

## 9. Paper Trading Audit

```text
Paper trades created: 0
Paper trade events: 0
Open positions: 0
Highest observed activity score: 52.75
Configured entry threshold: 80
```

Paper trading correctly did not fire during this window. There were no trade triggers to inspect for metric completeness, startup timing, stale CVD, impossible pressure, or opening/closing loops.

The absence of entries is consistent with the configured threshold. It does not prove entry safety under a real qualifying signal, especially while Binance depth/OI inputs are stale.

## 10. Findings

**P0**

1. **Binance Perpetual order books were frozen while stale values were persisted as fresh data.**
   - Evidence: one distinct BTC/ETH/SOL Binance Perp price for the full soak; only 1–2 distinct OBI values; 1,713 rows per symbol carried advancing timestamps.
   - Runtime evidence: 173 Binance Perp depth resyncs, including 12 sequence gaps, 6 HTTP 429s, and 155 HTTP 418s.
   - Exact paths:
     - `backend/app/collectors/orderbooks.py`, `BinanceOrderBookManager._bootstrap_all()` and `_synchronize_and_stream()`
     - `backend/app/runtime.py`, `LiveRuntime._collect_liquidities()`
   - Mechanism: one manager owns the entire Binance market group; a single symbol gap or failed bulk REST bootstrap restarts the group. The runtime retains the previously published book and writes it again using `datetime.now(UTC)`.
   - Minimal patch:
     - Partition Binance books into bounded independent chunks.
     - Limit snapshot REST concurrency/rate.
     - Invalidate or age-gate cached books during resync.
     - Exclude stale books from scoring, API aggregation, and persistence.

2. **Stale Binance derivatives were also persisted with fresh timestamps.**
   - Evidence: BTC/ETH each had one distinct Binance OI observation but hundreds of timestamp-advancing DB rows; SOL had no current Binance derivative observation. Binance OI windows never populated.
   - Exact paths:
     - `backend/app/collectors/derivatives.py`, `DerivativePollingCollector.run()`
     - `backend/app/runtime.py`, `LiveRuntime._build_detail()`
   - Mechanism: per-symbol pollers trigger sustained Binance 429/418 responses, while `_build_detail()` repeatedly persists `_latest_derivative()` with a new timestamp.
   - Minimal patch:
     - Globally rate-limit/stagger Binance derivative calls.
     - Persist only newly received source snapshots, using their source timestamp.
     - Age-gate stale derivative snapshots from funding/OI aggregation.

**P1**

1. **No-direction states are reported as `DIVERGENT`.**
   - Live evidence: SOL and AR reported both exchange directions as `NONE` while `cross_exchange_state` was `DIVERGENT`; 37 of 44 symbols were `DIVERGENT` in a sample where all 44 move types were `NEUTRAL`.
   - Exact path: `backend/app/scoring.py`, `cross_exchange_state()`.
   - The function returns `DIVERGENT` for every two-exchange case that is not the same non-`NONE` direction. This can distort downstream interpretation.

2. **Projected 30-day metric storage is approximately 87.7 GB.**
   - Retention is configured correctly, but the per-second, per-exchange, per-market cadence produces roughly 134 rows/second.
   - Capacity and pruning behavior should be validated before long-term collection.

**P2**

1. **Backend restart was not graceful within Docker’s stop timeout.**
   - It logged “Waiting for background tasks to complete,” was terminated with exit code 137, and then recovered successfully.

2. **Stream-success observability is incomplete.**
   - There are no explicit discovery-success, subscription-established, or connection counters. Successful trade/depth status had to be inferred from changing metrics and reconnect logs.

## 11. Known Limitations

- Runtime was 1h 12m 59s: enough for one 1h OI window, not a multi-day stability test.
- The 30-day pruning job was not awaited for 30 days; configuration, cutoff math, hourly cadence, and table scope were verified from the effective runtime path.
- No paper trade occurred, so qualifying-entry metric sanity was not exercised.
- Binance HTTP 418/429 behavior may depend partly on the test IP, but the implementation’s fan-out and group-wide restart behavior amplified it.
- Connection counts are inferred because the application does not log successful stream establishment.
- Selected aggregate price/liquidity snapshots include stale Binance book inputs and must not be treated as trusted market snapshots.
- No audit script was added; existing REST APIs and read-only SQL were sufficient.
- No thresholds, tests, features, or source files were modified.

## 12. Recommendation

```text
FIX_P0_BEFORE_CONTINUING
```

Do not begin long-term collection with the current build. Binance Perp order-book and derivative histories contain stale source values recorded under fresh timestamps, so persisted market data and any dependent pressure/activity signals cannot be trusted.
