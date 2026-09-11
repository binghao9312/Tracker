# T4 — Make the paper exit trigger venue-correct and stop-honest

Repo root is the current directory. Backend lives in `backend/`.

**The tests already exist and already fail. Your job is to make them pass by changing
production code only.** `backend/tests/test_paper_exit_semantics.py` is the
specification — read it first; it is more precise than this prose.

## Hard constraints
- Modify ONLY: `backend/app/paper_trading.py`
- Do NOT modify ANY file under `backend/tests/` — not the new test file, not
  `paper_helpers.py`, not any existing test. This is the whole point of the task;
  a change under `backend/tests/` fails the review even if the suite goes green.
- Do NOT touch `backend/app/api.py`, `backend/app/runtime.py`, `backend/app/repository.py`,
  `config/`, or `frontend/`
- No new third-party dependencies
- Do NOT change the database schema or the set of keys written to a trade row.
  Every field name already written by `_maintain_position` must still be written.

## Context the task depends on
- Run everything from `backend/`. The interpreter is `.venv/Scripts/python`
  (Windows layout under git-bash), e.g.
  `cd backend && ./.venv/Scripts/python -m pytest`. Do not create a new venv and do
  not try to install anything.
- Baseline before your change: **133 passed, 5 failed** (the 5 are the new file's
  venue and breached-stop cases; 3 in that file already pass and must stay passing).
- Everything lives in `_maintain_position` (around line 303) and `_reference_price`
  (around line 514). `_reference_price` currently has exactly one caller.
- `detail["price"]` is a CROSS-VENUE aggregate: `runtime._preferred_price()` prefers
  the Binance perp mid regardless of where the position was opened. The position row
  carries its own `position["exchange"]` and `position["market"]`.
- `_execution_book(detail, exchange=..., market=...)` already returns the book for a
  specific venue, and `_maintain_position` already uses it for the FILL. Only the
  TRIGGER decision uses the wrong price. Note it is called with the position's venue
  further down the same function — reuse that, do not add a second lookup pattern.
- `simulate_market_fill(...)` raises `FillUnavailable` on thin depth. When that
  happens today, `_maintain_position` returns a TRADE_SKIPPED event and leaves the
  position OPEN — so a position can survive a snapshot on which its stop was breached.
  That, plus restart recovery via `recover_open_positions`, is how a breached stop
  currently comes back as a TAKE_PROFIT.
- `SimulatedFill` is a frozen dataclass with `vwap`, `quantity`, `quote_notional`,
  `slippage`.

## Required behaviour

### 1. Trigger on the position's own venue
- Compute `reference_price` from the mid of the position's own
  `exchange`/`market` book: `(best_bid + best_ask) / 2`.
- If that book is absent or has no usable levels, make NO exit decision for this
  snapshot: return `None` without updating MFE/MAE and without closing. Do NOT fall
  back to `detail["price"]` — that fallback is the bug being removed.
- `_reference_price` should take the venue as arguments rather than growing a second
  function. Keep it a module-level helper.

### 2. A breached stop is never reported as a win
- The position's tracked `max_adverse_excursion_pct` is authoritative. If, after
  updating it with this snapshot, it is `<= -settings.stop_loss_pct`, the exit reason
  is `STOP_LOSS` — and that takes precedence over `TAKE_PROFIT` and `TIME_STOP`.
- Two distinct cases, and they must settle at different prices:
  - **The stop is breached by THIS snapshot** (`signed_return <= -stop_loss_pct`):
    unchanged behaviour. Simulate the fill against the venue book as today.
  - **The stop was breached EARLIER but this snapshot is better** (MAE breached,
    `signed_return > -stop_loss_pct`): the current favourable price must NOT be used,
    or you would book a profit labelled STOP_LOSS. Model the exit at the stop level:
    - LONG:  `exit_price = entry_price * (1 - stop_loss_pct)`
    - SHORT: `exit_price = entry_price * (1 + stop_loss_pct)`
    Build the `SimulatedFill` from that price and the position's existing `quantity`
    (`quote_notional = exit_price * quantity`, `slippage = 0.0`), then run it through
    the SAME pnl/fee/persistence path as a normal close. `return_pct` must be negative.
- Record in `exit_snapshot` that this fill was modelled rather than taken off the
  book — add a key such as `"exit_modelled": "STOP_LOSS_BREACH"` alongside the
  existing `"exit_fill"`. Do not remove or rename `"exit_fill"`.
- A take-profit with no stop breach must remain a `TAKE_PROFIT` with positive
  `return_pct`. Do not make every trade a stop.

### 3. Keep the rest intact
`TIME_STOP`, cooldown/arm-state handling, the `TRADE_SKIPPED` events, fee maths, and
every field written on close must behave as they do now.

## Acceptance
Run from the repo root and report the real output of each:

    cd backend && ./.venv/Scripts/python -m pytest
    cd backend && ./.venv/Scripts/python -m ruff check .
    cd backend && ./.venv/Scripts/python -m ruff format --check .
    cd backend && ./.venv/Scripts/python -m mypy app

All four must pass, with **138 passed** and 0 failed. No test file may be modified —
`git diff --name-only` must list `backend/app/paper_trading.py` and nothing else.

In your final report, paste: the pytest summary line, the mypy line, and the output of
`git diff --name-only`.
