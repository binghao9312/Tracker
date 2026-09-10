# Tracker Post-Fix Live Soak Validation Report

## 1. Commit Tested

```text
SHA: 26e260d2ce0728ea724141d8ca42881d6241620f
Start time: 2026-09-10T17:35:35.501664+00:00
End time: 2026-09-10T18:40:53.908090+00:00
Runtime duration: 1h 05m 18.406s
```

The working tree was clean before validation. The normal `docker compose up --build` stack was used without deleting the PostgreSQL volume. No production code, configuration, threshold, formula, schema, cadence, retention, exchange-list, Top-50, or frontend-design change was made.

## 2. Static Verification

```text
pytest: PASS — 104 passed, 1 warning in 3.19s
ruff: PASS — All checks passed!
frontend build: PASS — 36 modules transformed; Vite build completed in 921 ms
GitHub CI: PASS — run 18, conclusion success
```

GitHub evidence: [workflow run 34507562461](https://github.com/binghao9312/Tracker/actions/runs/34507562461) for the tested SHA.

## 3. Runtime Health

```text
backend: PASS — scanner and symbol APIs served live data on localhost:8001 for the full soak
frontend: PASS — Vite application served successfully on localhost:5174
PostgreSQL: PASS — container remained healthy; existing data was retained
market discovery: PASS — 46 active symbols discovered
```

The detailed sample set was `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, and `SEIUSDT`. `SEIUSDT` was selected from the live discovered universe at rank 50.

Backend container start was `2026-09-10T17:35:27.066443728Z`. First post-start Binance source observations for BTC/ETH/SOL appeared in 2.448–3.529 seconds for Spot books, 12.932–12.947 seconds for Perp books, and 1.934–2.934 seconds for derivatives. Live OKX books, trades, and derivatives were also present throughout sampling.

## 4. Binance Orderbook Health

| Market | Connections | Resyncs | Sequence Gaps | 429 | 418 | Final Status |
|---|---:|---:|---:|---:|---:|---|
| Spot | Not emitted | 0 | 0 | 0 | 0 | PASS — source timestamps remained live |
| Perp | Not emitted | 0 during soak; 1 after planned restart | 0 during soak; 1 after planned restart | 0 | 0 | PASS — transient restart resync recovered |

The configured log level did not emit initial connection events, so a connection count was not fabricated. There were zero observed disconnect/reconnect-loop messages and zero snapshot-retry messages during the uninterrupted soak. Direct API samples proved that Binance Spot and Perp source timestamps advanced. The single post-restart Binance Perp sequence-gap/resync occurred 6.4 seconds after backend start; subsequent Spot and Perp source timestamps both advanced over a five-second confirmation sample.

## 5. Binance Perp Freshness

Thirteen non-bootstrap samples per symbol were spread across approximately 60 minutes. All source and receive progressions were strictly monotonic.

| Symbol | Source timestamp progression (ms) | `received_at` progression (ms) | Distinct price | Distinct OBI | Distinct depth | Result |
|---|---|---|---:|---:|---:|---|
| BTCUSDT | 1789062029443 → 1789065634525 | 1789062029389 → 1789065634237 | 13/13 | 13/13 | 13/13 | PASS |
| ETHUSDT | 1789062029500 → 1789065634499 | 1789062029414 → 1789065634210 | 13/13 | 13/13 | 13/13 | PASS |
| SOLUSDT | 1789062042050 → 1789065634488 | 1789062041550 → 1789065634207 | 12/13 | 13/13 | 13/13 | PASS |

No many-minute frozen source timestamp occurred. Equal adjacent market values were not treated as failures; the source, OBI, and depth evidence independently changed.

## 6. Binance Derivative Health

| Symbol | Persisted successful observations | Distinct source/OI observations | Source progression | `received_at` progression (ms) | 429 | 418 | Result |
|---|---:|---:|---|---|---:|---:|---|
| BTCUSDT | 134 | 134/134 | 17:35:41.003Z → 18:40:08.000Z | 1789061728508 → 1789065609151 | 0 | 0 | PASS |
| ETHUSDT | 135 | 135/135 | 17:35:41.003Z → 18:40:46.000Z | 1789061728770 → 1789065599659 | 0 | 0 | PASS |
| SOLUSDT | 135 | 135/135 | 17:35:43.001Z → 18:40:49.001Z | 1789061730273 → 1789065603511 | 0 | 0 | PASS |

Across the discovered Binance universe, 5,376 genuine derivative source observations were persisted. There were 83 generic `derivative_collector_retry` events and zero cooldown events. The retries were clustered late in the soak, but produced no stale series or material data gap: across 9,520 consecutive derivative gaps for all exchange/symbol series, zero reached 60 seconds, the maximum was 52.847 seconds, and p99 was 49.000 seconds. BTC/ETH/SOL per-exchange maxima were 47.944–52.462 seconds. No derivative retries occurred in the measured post-restart window.

Persistence used source timestamps. There were zero duplicate `(exchange, symbol, timestamp)` keys for derivative rows during the soak, so a cached source observation did not become repeated fresh history. There were also zero duplicate `(exchange, symbol, market, timestamp)` market keys.

## 7. OI Windows

Values are final signed percentage changes. Every required 5m, 15m, and 1h value was non-null after the 65-minute runtime.

| Symbol | Exchange | 5m | 15m | 1h |
|---|---|---|---|---|
| BTCUSDT | Binance | PASS (`-0.029867%`) | PASS (`+0.017185%`) | PASS (`-0.030360%`) |
| BTCUSDT | OKX | PASS (`+0.006345%`) | PASS (`+0.205626%`) | PASS (`+0.363880%`) |
| ETHUSDT | Binance | PASS (`-0.023268%`) | PASS (`-0.086322%`) | PASS (`-0.407493%`) |
| ETHUSDT | OKX | PASS (`-0.154288%`) | PASS (`-0.024674%`) | PASS (`-0.570167%`) |
| SOLUSDT | Binance | PASS (`+0.047086%`) | PASS (`+0.155017%`) | PASS (`+0.094891%`) |
| SOLUSDT | OKX | PASS (`+0.159269%`) | PASS (`+0.284789%`) | PASS (`+0.089145%`) |

Signs remained signed; negative changes did not become positive, positive changes did not become negative, and unavailable short-window values remained null rather than zero.

## 8. Matched Pressure Validation

**PASS.** For every sampled selected-symbol/market payload where Binance and OKX both had fresh depth, aggregate pressure was independently recomputed as:

```text
aggregate buy pressure  = matched aggregate buy volume / matched aggregate ask depth
aggregate sell pressure = matched aggregate sell volume / matched aggregate bid depth
```

There were 428 live 1m/5m buy/sell comparisons. Maximum absolute error was `2.220446049250313e-16`, consistent with floating-point rounding. Therefore the numerator and denominator used the same fresh exchange set in every observed case.

Across 876 non-null per-exchange pressure values for the four selected symbols, zero were negative, NaN, or infinite. The maximum was `8.008028840290523`; its corresponding volume and depth were finite and valid.

No natural stale-book event occurred during this window. Live stale-event case not observed during this window. The passing deterministic regressions `test_stale_depth_keeps_trade_cvd_live_without_pressure` and `test_stale_flow_is_excluded_from_aggregate_pressure` provide the stale-path evidence without deliberately breaking a production feed. The full static suite also passed `test_market_persistence_uses_source_watermarks_and_stale_books` and `test_stale_binance_derivative_is_hidden_while_fresh_okx_remains_usable`.

Cross-exchange state semantics also passed. Across 644 scanner observations there were zero state mismatches: 512 `NEUTRAL`, 127 `SINGLE_EXCHANGE`, 4 `CONFIRMED`, and 1 `DIVERGENT`. Observed combinations included NONE+NONE, BUY+NONE, NONE+SELL, BUY+BUY, SELL+SELL, and BUY+SELL with their required states.

## 9. Trade Flow / CVD

Final aggregate CVD snapshot:

| Symbol | Spot CVD 1m | Spot CVD 5m | Perp CVD 1m | Perp CVD 5m |
|---|---:|---:|---:|---:|
| BTCUSDT | -98,438.87 | +117,854.47 | -277,138.81 | +7,405,553.90 |
| ETHUSDT | -347,791.58 | -519,609.71 | -1,214,392.65 | +15,731,768.00 |
| SOLUSDT | +2,532.10 | -13,376.92 | +655,599.35 | +3,277,157.39 |

For each BTC/ETH/SOL exchange/market series, all 14 sampled CVD 1m values and all 14 sampled CVD 5m values were distinct and non-zero. The 1m and 5m values were equal only at the initial bootstrap sample, then diverged and rolled independently. All sampled buy/sell volume windows were finite and non-negative. This confirms healthy Binance Perp trade flow as well as Spot flow.

## 10. Activity Distribution

Final distribution across 46 discovered symbols:

```text
min: 1.58
median: 11.28
p75: 12.9925
p90: 20.40
p95: 24.6425
max: 30.01
>=60: 0
>=70: 0
>=80: 0
>=90: 0
```

There were 45 distinct scores across 46 symbols. The distribution was neither all-zero, all-100, nor identical, and no feed-loss spike was observed. No scoring parameter was tuned.

## 11. Database Persistence

| Checkpoint | MarketMetricRow | FlowMetricRow | DerivativeMetricRow |
|---|---:|---:|---:|
| T0 | 2,987,559 | 3,001,551 | 724,921 |
| T+15m | 3,007,487 | 3,021,602 | 727,591 |
| T+30m | 3,019,017 | 3,033,175 | 730,015 |
| T+45m | 3,027,806 | 3,041,977 | 732,177 |
| T+60m | 3,034,486 | 3,048,660 | 733,950 |
| T+65m | 3,036,275 | 3,050,453 | 734,444 |

Approximate full-window growth rates were:

```text
market rows/sec: 12.43
flow rows/sec: 12.48
derivative rows/sec: 2.43
```

These are lower than the prior problematic soak's approximately 55.6, 55.5, and 23.0 rows/second, respectively. That direction is expected after source-watermark persistence stopped cached/stale snapshots from producing duplicate observations. All three tables grew at every checkpoint; none plateaued.

Restart persistence: **PASS**. Before restart, the latest BTC history IDs captured from the API were market `3037943`, flow `3052229`, and derivative `734921`. After restarting only the backend, all three pre-restart IDs remained available from the historical endpoint and each series contained newer IDs. Database totals grew by 2,633 market, 2,928 flow, and 178 derivative rows during the restart verification interval. The PostgreSQL volume was not removed.

## 12. Paper Trading Audit

```text
positions opened: 0
stale-trigger evidence: none; no entry occurred
result: PASS — no trade triggered during validation window
```

All 14 position samples were empty, and final paper statistics reported zero total and zero open trades. No live-order credentials were present in the backend environment and no live order was placed. Trading thresholds, TP/SL, and scoring were unchanged.

## 13. Errors

```text
HTTP 429: 0
HTTP 418: 0
sequence gaps: 0 during soak; 1 transient immediately after planned restart
resyncs: 0 during soak; 1 transient immediately after planned restart
stale events: 0
recoveries: 0 explicit recovery messages
connection reset / websocket disconnected: 0
snapshot retries: 0
derivative retries: 83 during soak; 0 in measured post-restart window
unexpected exceptions: 0 Traceback; 0 Unhandled
```

Classification:

- Transient: the single planned-restart Binance Perp sequence gap/resync; source timestamps advanced afterward.
- Repeating: 83 blank `derivative_collector_retry:` warnings, concentrated in late-window bursts. They were not 429/418 responses and did not create a gap of 60 seconds or more.
- Persistent: none. No feed froze, no reconnect/resync loop formed, and no exception terminated the backend.

## 14. Findings

**P0**

- None. No stale data was persisted as fresh, no Binance book or OI series froze, no source timestamp was rewritten as fresh, matched pressure remained exact, and PostgreSQL persistence survived restart.

**P1**

- None. The derivative retries were recoverable, were not rate limiting, and caused no material data gap across the monitored universe.

**P2**

- Derivative retry warnings contain neither exception text nor exchange/symbol identity. This prevented direct attribution of the 83 retries even though source-gap and persistence evidence showed continued health.
- Initial connection events are not emitted at the configured log level, so exact connection counts were not observable from logs. Direct source-timestamp progression was used as the live-connection evidence.

## 15. Known Limitations

- No natural stale orderbook or derivative event occurred; stale exclusion relies on the named deterministic regression tests plus zero duplicate source timestamps in live persistence.
- No paper position opened, so a live paper-entry trigger could not be audited. The stale-entry regression path passed in the static suite.
- Exact connection-attempt counts and per-exchange derivative retry attribution are unavailable with current logging.
- The uninterrupted live window was 65 minutes. It validates the preferred soak duration, not multi-day exchange/network behavior or retention pruning.
- Detailed freshness and flow inspection covered BTCUSDT, ETHUSDT, SOLUSDT, and rank-50 SEIUSDT; activity distribution and derivative-gap checks covered the full 46-symbol discovered universe.

## 16. Recommendation

```text
READY_FOR_LONG_TERM_COLLECTION
```

All required readiness conditions passed: Binance Spot and Perp trade feeds were healthy; Binance Perp depth and derivatives remained live; no sustained 429/418 storm occurred; stale books and derivatives are excluded by the passing regression paths; no stale source was repeatedly persisted; matched pressure held for all live comparisons; PostgreSQL survived restart and resumed appending; and no new P0 was found. The P2 logging gaps should be corrected during normal maintenance without changing collection readiness.
