# Task T4: backend/research/costs.py — execution cost model

Create the module that `backend/tests/test_costs_contract.py` imports. Those tests
already exist and currently fail with `ModuleNotFoundError`. They are the complete
specification: implement exactly what they require, nothing more.

This model decides whether a candidate trading signal is worth trading, so a wrong
number here does not crash — it silently justifies or kills a strategy. That is why
the arithmetic is pinned by tests written before the implementation.

## Hard constraints

- Create ONLY `backend/research/costs.py`.
- Do NOT modify anything under `backend/tests/`. Those tests are the oracle; editing
  them removes the only thing that can tell either of us the code is right. If you
  believe a test is wrong, stop and say so instead of changing it.
- `backend/app/` must never import from `backend/research/`.
- No new third-party dependencies. Standard library only — this is pure arithmetic,
  it does not need numpy.
- Do NOT run `uv`, `pip install`, or `poetry`, and do not create or modify lock files.

```scope
backend/research/costs.py
```

## Context

**Every shell command must be non-interactive or it will hang forever.** `git` needs
`--no-pager`.

Run everything from `C:\Users\HAO\Documents\Qtrade\backend` using this interpreter
(the bare `python` on PATH is a different install and lacks the dependencies):

```
./.venv/Scripts/python.exe
```

Read `backend/tests/test_costs_contract.py` first. Its module docstring carries the
measured inputs and the sign convention, and the test names state the intended
meaning of each function. The public surface it imports is:

- `FeeSchedule` — carries `maker_bps` and `taker_bps` as attributes, plus a
  `with_discount(fraction)` method returning a new schedule with both sides scaled
  down by that fraction.
- `taker_round_trip_bps(fees, spread_bps)`
- `maker_round_trip_bps(fees, spread_bps, adverse_selection_bps)`
- `blended_round_trip_bps(fees, spread_bps, fill_rate, adverse_selection_bps)`
- `breakeven_alpha_bps(round_trip_cost_bps)`

Two conventions that the tests depend on and that are easy to get backwards:

- **Positive means cost.** A passive fill that earns the spread makes the number
  smaller, and can legitimately make it negative when fees are a rebate.
- **A round trip crosses the spread once in total, not twice.** Buying at the ask
  costs half a spread against the mid and selling at the bid costs the other half.
  The taker case therefore adds one full `spread_bps`; the filled-maker case
  subtracts one full `spread_bps` because it collects that half twice instead.
  Adverse selection, by contrast, is charged once per side.

Note that `FeeSchedule` must accept negative `maker_bps` — high VIP tiers and some
venues pay makers a rebate, and a validation rule that rejects it would be wrong.
`with_discount` and `fill_rate` do have valid ranges and the tests check both.

## Required behaviour

Everything observable is defined by `backend/tests/test_costs_contract.py`. Beyond
passing it:

- Keep the module free of I/O, configuration lookups and global state. It should be a
  handful of pure functions over a small frozen dataclass, so that a future caller can
  sweep fee tiers and fill rates cheaply.
- Type-annotate the public functions; the repo runs `ruff` and is fully annotated.
- Docstrings should say what a number *means* to a caller deciding whether to trade,
  not restate the formula. Whoever reads this next needs to know that
  `breakeven_alpha_bps` is the bar a signal's gross alpha has to clear.

## Acceptance

```acceptance
cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_costs_contract.py -q
cd backend && ./.venv/Scripts/python.exe -m pytest -q
cd backend && ./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m ruff format --check .
```

All three must exit 0. The first currently fails at import with
`ModuleNotFoundError: No module named 'research.costs'`.

Report at the end:
1. The `pytest -q` summary line for the whole suite.
2. The value your implementation returns for
   `taker_round_trip_bps(FeeSchedule(2.0, 5.0), spread_bps=1.4)` and for
   `maker_round_trip_bps(FeeSchedule(2.0, 5.0), spread_bps=1.4, adverse_selection_bps=-0.3)`.
3. Any test whose intent you found ambiguous, and how you resolved it.
