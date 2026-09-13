# Task T2: make RollingTradeFlow.windows() cost independent of trade volume

`backend/app/flow.py` holds a rolling one-hour trade history per exchange-market-symbol
pair and answers rolling-window volume/CVD queries from it. `windows()` currently
rescans the stored trades on every call, so its cost grows with market activity.

That query sits on a 1-second cadence loop and runs once per pair (~163 pairs live), so
its cost is multiplied by 163 before it meets a 1-second budget. Measured on this
machine on 2026-09-12:

| trades/s per pair | trades held | windows() per call | x163 pairs | vs 1s budget |
|---|---|---|---|---|
| 5   | 18,000  | 0.47 ms  | 77 ms   | 0.1x |
| 20  | 72,000  | 1.87 ms  | 304 ms  | 0.3x |
| 50  | 180,000 | 5.55 ms  | 905 ms  | 0.9x |
| 100 | 360,000 | 10.72 ms | 1747 ms | 1.7x |

In production this ran the backend to 99.5% CPU, stretched the cadence loop from its
configured 1 second to one flush per 61 seconds, and grew BTCUSDT write gaps from 13.8s
to 278s over 16 hours. Ingestion is not the problem: `add_trade` costs 2.2 us in-order
and 4.6 us with late arrivals, which is comfortably inside budget. Only `windows()` is.

Your job is to make a query's cost independent of how many trades are being held, while
keeping every answer byte-identical to what the current implementation returns.

## Hard constraints

- Modify ONLY `backend/app/flow.py`.
- Do NOT modify any file under `backend/tests/` — the tests are the oracle for this
  task, so editing them would remove the only thing that can tell either of us the
  rewrite is correct. If you believe a test is wrong, stop and say so instead.
- Do NOT change the public API. `runtime.py:723` calls
  `flow.windows(now_ms, requested_seconds=(60, 300))` and reads `.buy_volume`,
  `.sell_volume`, `.delta`, `.buy_sell_ratio`, `.cvd` off each `FlowWindow`.
  Keep `RollingTradeFlow`, `FlowWindow`, `WINDOWS_SECONDS`, `pressure()`, and the
  `add_trade` / `prune` / `windows` method names and signatures exactly as they are.
- No new third-party dependencies. Standard library only.
- Do NOT run `uv`, `pip install`, or `poetry`, and do not create or modify lock files.
  Use the interpreter given below, which already has everything.

```scope
backend/app/flow.py
```

## Context

**Every shell command must be non-interactive or it will hang forever.** `git` needs
`--no-pager`.

Run everything from `C:\Users\HAO\Documents\Qtrade\backend`. Use this interpreter
(the bare `python` on PATH is a different install and lacks the dependencies):

```
./.venv/Scripts/python.exe
```

Behaviour that currently holds and must continue to hold:

- `WINDOWS_SECONDS = (10, 60, 300, 900, 3600)`. `windows()` accepts a
  `requested_seconds` subset and raises `ValueError` for anything outside that tuple.
  Asking for a subset must give the same numbers as asking for everything.
- A window is half-open on the old side: a trade whose timestamp is *exactly*
  `now_ms - window*1000` is **inside** the window. One millisecond older is outside.
- Retention is one hour. `prune()` drops trades strictly older than
  `timestamp_ms - 3_600_000`, and is currently called from both `add_trade` and
  `windows()`.
- `buy_sell_ratio` is `None` when sell volume is zero — deliberately undefined rather
  than infinite. `cvd` and `delta` are both `buy - sell`.
- Timestamps are integer milliseconds.

**The part that is easy to get wrong:** trades do not always arrive in timestamp order.
Two venues share one clock and late arrivals are routine — `add_trade` has an explicit
insertion path for them. Any scheme built on running totals or cumulative sums has to
stay correct when a trade lands *behind* the newest one already recorded, including
when it lands in a bucket the running totals have already passed. The tests drive a
stream with 15% late arrivals specifically to catch this.

## Required behaviour

The observable contract is unchanged; only the cost changes. Specifically:

- The cost of a `windows()` call must not grow with the number of trades held. The
  test allows 8x the trades to cost at most 3x the time; keeping running totals in
  fixed-width time buckets gets this to roughly 1x, but the approach is yours.
- `add_trade` must stay cheap. It is called far more often than `windows()`, so moving
  the whole cost from query into ingestion is not a fix — it must not become linear in
  the number of trades held.
- Correctness is defined by `backend/tests/test_flow_window_contract.py`, which
  compares against a naive reference implementation over randomised streams. If your
  implementation and the reference disagree, yours is wrong.

Floating-point note: if you accumulate incrementally rather than summing from scratch,
tiny ordering differences in float addition are expected and fine — the tests compare
to 6 decimal places. But do not let error accumulate unboundedly across a long run;
if you subtract expired volume from a running total, think about whether drift can
grow without bound and say what you concluded.

## Acceptance

```acceptance
cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_flow_window_contract.py -q
cd backend && ./.venv/Scripts/python.exe -m pytest -q
cd backend && ./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m ruff format --check .
```

All three must exit 0. The first is the one that matters: it currently fails only on
`test_query_cost_does_not_grow_with_trade_volume`, at a ratio of about 8.3x.

Report at the end:
1. The measured ratio the scaling test reports after your change.
2. The full `pytest -q` summary line (it should be 183 passed).
3. Which data structure you used, in two or three sentences, and how it stays correct
   when a trade arrives out of order.
4. Your conclusion on float drift.
