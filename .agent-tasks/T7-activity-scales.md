# T7 — Make activity-score saturation scales configurable

Repo root is the current directory. Backend lives in `backend/`.

**The tests already exist and already fail: `backend/tests/test_activity_scales.py`.
They are the specification -- read them first, they are more precise than this prose.**

## Why this change exists
Measured against 2.2 hours of live data, three of the five score components could not
reach their weight:

| component | weight | old saturation | observed p99 | earned |
|---|---|---|---|---|
| pressure (spot half) | 50 shared | 3.0 | 0.94 | pinned near 0 |
| pressure (perp half) | 50 shared | 3.0 | 2.39 | partial |
| oi_change | 10 | 1.0 (= 100% in 5 min) | 0.0076 | **0.08** |
| funding | 5 | 0.001 via scale=1000 | 0.000277 | ~1.4 |

So the effective ceiling was ~60/100 and the configured entry threshold of 80 was
unreachable whenever both spot and perp books existed.

## Hard constraints
- Modify ONLY: `backend/app/scoring.py`, `backend/app/runtime.py`,
  `config/scoring.yaml`, `README.md`
- **Do NOT modify ANY file under `backend/tests/`.** Not the new file, not the
  existing ones. Several existing tests assert exact scores; they are expected to
  keep passing unchanged, and if one fails, that is information about your change,
  not a reason to edit it.
- Do NOT change the component WEIGHTS (50 / 30 / 10 / 5 / 5), the 100-point cap, the
  direction-neutral `abs()` semantics, or the `_available_mean` behaviour that
  excludes an absent market instead of scoring it zero.
- Do NOT change the entry/rearm thresholds in `config/scoring.yaml`. Threshold
  recalibration is a separate, data-driven decision that happens after this lands.
- No new dependencies.

## Context
- Run from `backend/`. Interpreter is `.venv/Scripts/python` (Windows layout under
  git-bash): `cd backend && ./.venv/Scripts/python -m pytest`. Do not create a venv,
  do not install anything.
- Baseline: **165 passed** plus the new file failing at import. Everything that
  passes now must still pass.
- **Do not touch Docker.** Do not run `docker compose`, do not build an image, do not
  restart or stop a container, and do not write to any database. A live backend
  container is collecting data right now and must not be interrupted. Your change
  reaches it only via a future rebuild, which is not part of this task.
- `activity_score` is at `backend/app/scoring.py:308`; `_market_pressure` at 358 and
  `_bounded_magnitude` at 377 are the helpers that hold the hardcoded scales.
- `config/scoring.yaml` already has `classification`, `trade_signal`,
  `data_retention` and `paper_trading` sections, and `backend/app/scoring.py` /
  `backend/app/trade_signal.py` already contain strict loaders for the first two.
  **Follow those loaders exactly**: missing key -> explicit `ValueError` naming it,
  unknown key -> `ValueError`, non-finite or out-of-range -> `ValueError`.
- The one call site is `LiveRuntime._build_detail` in `backend/app/runtime.py`
  (around line 612).

## Required behaviour

1. `ActivityScoreScales` frozen dataclass in `scoring.py` with exactly these fields,
   in this order, all floats:
   `spot_pressure_saturation`, `perp_pressure_saturation`, `oi_change_saturation`,
   `funding_saturation`.
   Validate in `__post_init__`: each must be finite and strictly greater than zero.
   A zero would divide by zero and make every reading saturate.

2. A module-level default instance carrying the values below, used when no `scales`
   argument is passed, so every existing caller keeps working.

3. `activity_score(..., scales: ActivityScoreScales = <default>)` -- a keyword-only
   argument added at the end. A component reading at exactly its saturation earns
   exactly its full weight; below saturation it scales linearly; above it is capped.
   Spot and perp pressure each use their OWN saturation.

4. `load_activity_score_scales(path)` reading a new `activity:` section, with the
   same strictness as the neighbouring loaders.

5. `config/scoring.yaml` gains:

   ```yaml
   activity:
     spot_pressure_saturation: 1.0
     perp_pressure_saturation: 3.0
     oi_change_saturation: 0.02
     funding_saturation: 0.0005
   ```

   These are **my values, derived from the observed distributions; do not change
   them and do not "improve" them.** Each sits at or slightly above the observed p99
   for that input, so an unusually extreme reading saturates while a typical one does
   not. They are provisional pending a volatility event, which is exactly why they
   are configuration.

6. `LiveRuntime` loads the scales once (alongside how it already loads its other
   configuration -- do not read the file per cadence) and passes them to
   `activity_score`. If the runtime takes them as a constructor argument, give it a
   default so existing construction sites in tests keep working unchanged.

7. README: note under Configuration that `activity.*` sets the saturation point of
   each score component, that a component at its saturation earns its full weight,
   and that these are calibrated from observed distributions and should be re-derived
   from a longer window that includes a volatility event.

## Acceptance
Run from the repo root and report the real output of each:

    cd backend && ./.venv/Scripts/python -m pytest
    cd backend && ./.venv/Scripts/python -m ruff check .
    cd backend && ./.venv/Scripts/python -m ruff format --check .
    cd backend && ./.venv/Scripts/python -m mypy app research

All four must pass, with every test in `tests/test_activity_scales.py` passing and
zero failures anywhere. `git status --short` must show only the four allowed files --
if anything under `backend/tests/` appears, the task has failed regardless of the
test count.

In your final report paste: the pytest summary line, the mypy line,
`git status --short`, and the `activity:` block as it ended up in `config/scoring.yaml`.
