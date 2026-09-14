# Task T7: make backend/research/panel.py stream instead of buffering the window

`backend/research/panel.py` already exists and is correct: all thirteen tests in
`backend/tests/test_research_fidelity.py` pass. A fourteenth test,
`PanelScalingTests::test_memory_does_not_track_row_density`, currently fails. Make it
pass **without breaking any of the other thirteen.**

## The problem, measured

`build_panel` loads every row in the window into memory as ORM objects before touching
the grid. Measured on a 338,400-row database: 566 MB of peak traced allocation. A real
day is about 4.7M rows across the four tables (46 symbols at a ~1.7s cadence), which
extrapolates to roughly **7.9 GB**. The failing test reproduces the shape in miniature:
holding the grid fixed and making the rows behind it 8x denser currently costs 8.6x the
memory.

This is the difference between the research loop being usable on a day of data and not,
which is the whole point of the module.

## The approach that should work

Both the rows and the grid are ordered by time. One forward pass can walk them together:
advance through the rows up to each grid point, keeping only the latest row per
(symbol, exchange, market), and discard everything behind. Memory then depends on the
panel being built — grid x symbols — rather than on how long the collector was running.

Concretely, what tends to matter:

- Stream the result set rather than materialising it. SQLAlchemy's
  `.execution_options(yield_per=...)` (or `stream_results`) exists for this; with the
  sync SQLite session used by the tests, iterating the result without building a list is
  what counts.
- Select the **columns you need**, not whole ORM entities. A `select(Table.c.a, Table.c.b)`
  row is a lightweight tuple; a hydrated ORM object with its identity-map entry is not.
  This alone is a large fraction of the difference.
- Do not keep a per-symbol index of every row's timestamp. The current
  `_group_rows` / `_by_symbol` structure is the buffering, just spread across
  dictionaries.

You do not have to follow any of this. The test is the requirement; the route is yours.

## Hard constraints

- Modify ONLY `backend/research/panel.py`.
- Do NOT modify anything under `backend/tests/`. If you believe a test is wrong, stop
  and say so instead of changing it. (The coverage assertion in that file was wrong
  once already and was fixed by me after a delegate reported it, so this is a real
  option, not a formality.)
- Do NOT weaken the panel to pass the test. Narrowing the window, sampling rows,
  dropping features, or lowering precision would all make the number smaller and the
  module useless. Every one of the other thirteen tests must still pass unchanged, and
  they check the values cell by cell against a hand rebuild.
- `backend/app/` must never import from `backend/research/`, and the scoring arithmetic
  must keep coming from the real `app.scoring` / `app.trade_signal` functions.
- No new third-party dependencies; no pandas.
- Do NOT run `uv`, `pip install`, or `poetry`, and do not create or modify lock files.

```scope
backend/research/panel.py
```

## Context

**Every shell command must be non-interactive or it will hang forever.** `git` needs
`--no-pager`.

Run everything from `C:\Users\HAO\Documents\Qtrade\backend` using this interpreter:

```
./.venv/Scripts/python.exe
```

The failing test is at the bottom of `backend/tests/test_research_fidelity.py`; its
docstring carries the measurements above.

## Acceptance

```acceptance
cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_research_fidelity.py -q
cd backend && ./.venv/Scripts/python.exe -m pytest -q --ignore=tests/test_research_evaluate.py
cd backend && ./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m ruff format --check .
```

All three must exit 0. `tests/test_research_evaluate.py` is ignored on purpose: it is
another task's oracle and its module does not exist yet.

Report at the end:
1. The `pytest -q` summary line.
2. The memory ratio the scaling test now reports, and the peak figures behind it.
3. What you changed, in a sentence or two — the mechanism, not a list of edits.
4. Anything in the test that looks wrong to you.
