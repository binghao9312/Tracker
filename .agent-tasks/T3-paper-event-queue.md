# T3 — Stop silently dropping paper-trading events in the WebSocket fan-out

Repo root is the current directory. Backend lives in `backend/`.

## Hard constraints
- Modify ONLY: `backend/app/api.py`
- Create ONLY: `backend/tests/test_paper_event_queue.py`
- Do NOT modify any existing test file. Do NOT touch `backend/app/runtime.py`,
  `backend/app/paper_trading.py`, or anything under `frontend/` or `config/`
- No new third-party dependencies

## Context the task depends on
- Run everything from `backend/`. The virtualenv interpreter is `.venv/Scripts/python`
  (Windows layout, git-bash). Use it explicitly, e.g.
  `cd backend && ./.venv/Scripts/python -m pytest`. Do not create a new venv.
- `DashboardState` in `backend/app/api.py` holds
  `self._subscribers: defaultdict[str, set[asyncio.Queue]]`.
- `subscribe(channel)` (around line 137) currently creates
  `asyncio.Queue(maxsize=1)` for EVERY channel, and `_broadcast` (around line 146)
  does `if queue.full(): queue.get_nowait()` — i.e. drop-oldest.
- Channels in use: `"scanner"`, `f"symbol:{symbol}"`, and `"paper"`.
- Drop-oldest is CORRECT for `scanner` and `symbol:*` — those carry a latest-value
  snapshot, where a superseded value is worthless. It is WRONG for `paper`, which
  carries a discrete event stream (`TRADE_OPENED`, `TRADE_CLOSED`, `TRADE_SKIPPED`,
  `SIGNAL_TRIGGERED`); dropping one loses a trade record permanently and silently.
- `_broadcast` is called from `publish()` on the 1-second cadence and must never block
  the event loop waiting for a slow client.
- The existing WS endpoints are defined near the bottom of `create_app`; find how
  `subscribe` is consumed there before changing its signature.

## Required behaviour
1. Give `subscribe` an explicit per-channel queueing policy. Latest-value channels keep
   `maxsize=1` drop-oldest. The `paper` channel gets a bounded event queue with
   `maxsize=256` that does NOT drop.
2. When a non-dropping queue is full, the slow subscriber must be **disconnected**
   rather than silently losing an event: mark that queue as overflowed, remove it from
   `self._subscribers`, and make its `subscribe()` generator terminate (so the
   WebSocket endpoint closes and the browser reconnects and re-fetches via REST).
   Losing the connection is recoverable; losing an event is not.
3. `_broadcast` must stay non-blocking — no `await queue.put(...)` on a full queue.
4. Log a warning when a subscriber is dropped for overflow, including the channel name.
5. Events already queued before an overflow must still be delivered in FIFO order if
   the consumer drains them; ordering must never be reordered or deduplicated.
6. Behaviour for `scanner` and `symbol:*` must be unchanged — those still coalesce to
   the newest message under load.

## Required tests (`backend/tests/test_paper_event_queue.py`)
Use `pytest` with `asyncio` directly (look at how existing async tests in
`backend/tests/` are written — follow the same style; do not add pytest plugins).
Cover at least:
- A `paper` subscriber that stays idle while 300 events are broadcast is dropped from
  `_subscribers`, and its generator stops — rather than quietly losing events.
- A `paper` subscriber consuming normally receives **every** event, in order, for a
  burst of 100 events (this is the regression that matters).
- A `scanner` subscriber that stays idle still coalesces: after several broadcasts it
  receives the most recent message, and old ones are dropped.
- `_broadcast` never blocks: broadcasting to a full non-draining queue returns promptly.

## Acceptance
Run from the repo root and report the real output of each:

    cd backend && ./.venv/Scripts/python -m pytest -q
    cd backend && ./.venv/Scripts/python -m ruff check .
    cd backend && ./.venv/Scripts/python -m ruff format --check .
    cd backend && ./.venv/Scripts/python -m mypy app

All four must pass. The pre-existing baseline is **113 passed, 20 skipped** — the 20
skips are an unrelated numpy-gated research module, leave them alone. Your new tests
must ADD to the passing count; no existing test may fail or be modified.

In your final report, paste: the pytest summary line, the mypy line, and
`git diff --stat`.
