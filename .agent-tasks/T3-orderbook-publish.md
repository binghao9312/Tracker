# Task T3: stop LocalOrderBook.to_model() sorting the whole book on every publish

`backend/app/orderbook.py` keeps a local order book as two `dict[Decimal, Decimal]`
and exposes `bids` / `asks` as properties that call `sorted()` over the entire book.
`to_model()` reads both, slices the top `max_levels`, and builds a pydantic `OrderBook`.

This is the dominant CPU cost in production. A 45-second sampled profile of the live
backend attributed 73% of all CPU time to exactly two lines:

```
45.89%  bids (app/orderbook.py:135)
27.24%  asks (app/orderbook.py:139)
```

`_publish` throttles to 10Hz per symbol and there are ~163 exchange-market-symbol
pairs live, so `to_model()` runs up to ~1,630 times a second. Measured cost of just
the two sorts, against a one-core budget:

| levels/side | full sort x2 | cores needed at 163 pairs x 10Hz |
|---|---|---|
| 1,000 (the Binance snapshot size) | 0.71 ms | 1.2 |
| 2,000 | 1.66 ms | 2.7 |
| 5,000 | 5.11 ms | 8.3 |
| 10,000 | 15.52 ms | 25.3 |

Two separate problems are visible there.

**It is over budget before anything degrades.** At the snapshot size the sorting alone
needs more than one core.

**It then gets worse for as long as the process runs.** Binance `@depth@100ms` is a
*diff* stream. A price level is only removed from the local book when an update
carries quantity zero for it, and levels far from the mid rarely receive one. So the
book accumulates stale depth indefinitely and every publish sorts all of it. In
production this ran the backend to 99.5% CPU, stretched a 1-second cadence loop to one
flush per 61 seconds, grew BTCUSDT write gaps from 13.8s to 278s over 16 hours, and
previously ended in a SIGSEGV.

## Hard constraints

- Modify ONLY `backend/app/orderbook.py`.
- Do NOT modify anything under `backend/tests/`. The tests are the oracle for this
  task; editing them removes the only thing that can tell either of us the change is
  correct. If you think a test is wrong, stop and say so rather than changing it.
- Keep the public API exactly as it is: `LocalOrderBook`, `SequencedOrderBookSnapshot`,
  `OrderBookSequenceGap`, the `synchronized` / `sequence` / `symbol` / `bids` / `asks`
  properties, `bootstrap`, `apply_binance_spot_update`, `apply_binance_futures_update`,
  `apply_okx_update`, and `to_model(timestamp, *, received_at=None, max_levels=200)`.
- All existing sequence-gap and invalidation behaviour must be preserved exactly.
  `backend/tests/test_binance_spot_depth_sequence.py` and the other existing order-book
  tests already cover this and must keep passing.
- No new third-party dependencies. Standard library only. In particular do NOT add
  `sortedcontainers`.
- Do NOT run `uv`, `pip install`, or `poetry`, and do not create or modify lock files.

```scope
backend/app/orderbook.py
```

## Context

**Every shell command must be non-interactive or it will hang forever.** `git` needs
`--no-pager`.

Run everything from `C:\Users\HAO\Documents\Qtrade\backend`, using this interpreter
(the bare `python` on PATH is a different install and lacks the dependencies):

```
./.venv/Scripts/python.exe
```

Things worth knowing before you choose an approach:

- Prices and quantities are `Decimal`. `Decimal.__lt__` is far more expensive than a
  float comparison, and it is executed O(n log n) times per publish. Preserving exact
  decimal values in the *output* matters; using a cheaper key for *ordering* is your
  call, as long as ordering stays correct for the values these venues actually send.
- `to_model` is the only consumer of published depth. `runtime.py` and `liquidity.py`
  both read the truncated `OrderBook` model, never the raw book, and every caller uses
  `max_levels=200`.
- Updates are applied through `_apply_levels`, which is cheap (0.75% of profile) and is
  not the problem. Do not move the cost from publish into ingestion: ingestion runs far
  more often than publishing.

**The part that is easy to get wrong:** bounding the book is a real behaviour change,
because a discarded level can never come back except via a fresh snapshot. The
guarantee you must preserve is that discarding is invisible at the published depth.
`backend/tests/test_orderbook_snapshot_contract.py` states the boundary: retain on the
order of 1,000 levels per side so there is roughly 800 levels of headroom behind the
published 200. Discarding must always take the levels *furthest from the mid* — never
the best ones — and a bound tighter than `MIN_RETAINED_PER_SIDE` in that test will fail
`test_deep_levels_still_reachable_by_asking_for_more`.

## Required behaviour

The published output is unchanged; the cost changes. Concretely:

- `to_model()` cost must not grow with how long the process has been running, i.e. not
  with the number of diff updates applied. The test allows 20x the updates to cost at
  most 3x the time; it currently measures 15.3x.
- The retained book must stay bounded under a diff stream that keeps introducing new
  far-from-mid levels and never zeroes them.
- Publishing a fixed number of levels should not require ordering the whole book.
  `heapq.nlargest` / `nsmallest` is O(n log k) rather than O(n log n) and is a fine
  building block, but on its own it does not fix the growth problem — measured, it
  only improves the 10,000-level case from 15.52ms to 2.51ms, still far over budget.
  Something has to stop the book growing as well.
- Correctness is defined by `backend/tests/test_orderbook_snapshot_contract.py`, which
  compares published levels against a reference book that keeps everything and sorts
  naively. If your implementation and the reference disagree, yours is wrong.

## Acceptance

```acceptance
cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_orderbook_snapshot_contract.py -q
cd backend && ./.venv/Scripts/python.exe -m pytest -q
cd backend && ./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m ruff format --check .
```

All three must exit 0. The first currently fails on exactly two tests:
`test_book_does_not_grow_without_bound` and
`test_publish_cost_does_not_grow_with_accumulated_updates`. The other seven pass now
and must still pass afterwards.

Report at the end:
1. The ratio reported by the scaling test after your change, and the retained level
   count reported by the growth test.
2. The full `pytest -q` summary line.
3. What you changed, in three or four sentences: how ordering is done now, what bounds
   the book, and why a discarded level can never be one that would have been published.
4. Anything you had to trade off, especially any case where your version could differ
   from the reference book.
