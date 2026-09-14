# Task T9: trailing-volatility baseline and partial rank IC

Extend `backend/research/evaluate.py` so that `backend/tests/test_research_baseline.py`
passes. That test already exists and currently fails with `ImportError`. It is the
complete specification: read it first and in full, including its module docstring, which
explains what the comparison is for.

This decides whether Phase E happens at all. `activity_score` predicting the *size* of
the next move is the only positive result this system has, and it has never been
compared against the obvious alternative: volatility clusters, so last period's realised
volatility predicts next period's for free. If the baseline scores as well, the result is
a rediscovery of volatility clustering and Phase E should be abandoned. A number that is
wrong in the optimistic direction here buys weeks of wasted work.

## Hard constraints

- Modify ONLY `backend/research/evaluate.py`. Add to it; do not restructure it.
- Do NOT modify anything under `backend/tests/`, and do not modify any other file. If you
  believe a test is wrong, stop and say so instead of changing it. Three earlier tasks in
  this repo found real errors in my tests that way, so this is a genuine option and not a
  formality.
- **Every existing test must still pass**, including `tests/test_research_evaluate.py`,
  which pins the current `rank_ic` behaviour. The new `control` argument is additive:
  callers that do not pass it must get exactly what they get today.
- `backend/app/` must never import from `backend/research/`.
- Reuse what is already in `research/forward.py` and the existing `_rankdata` /
  `_spearman` helpers in `evaluate.py`. Do NOT add a second ranking implementation.
- No new third-party dependencies. `numpy` only; no pandas, no scipy.
- Do NOT run `uv`, `pip install`, or `poetry`, and do not create or modify lock files.

```scope
backend/research/evaluate.py
```

## Context

**Every shell command must be non-interactive or it will hang forever.** `git` needs
`--no-pager`.

Run everything from `C:\Users\HAO\Documents\Qtrade\backend` using this interpreter:

```
./.venv/Scripts/python.exe
```

## Required interface

```python
def trailing_volatility(panel, window_seconds: int) -> np.ndarray:
    """(T, S) realised volatility of log price over the trailing window."""

def rank_ic(
    panel,
    feature: str | np.ndarray,          # was: str
    horizon_seconds: int,
    *,
    within_symbol: bool = True,
    absolute: bool = True,
    tradable_only: bool = True,
    control: str | np.ndarray | None = None,   # new
) -> ICResult: ...
```

## Semantics

**`trailing_volatility`.** At each grid row, the standard deviation of the log returns
observed over the preceding `window_seconds`. Strictly backward-looking: the value at row
`t` must use no price after `t`. A forward-looking baseline would beat everything and
mean nothing, and the test checks this directly with a flat-then-violent path.

Rows with too little history are `NaN`, not zero — zero reads as a real observation of
"perfectly calm" and would be ranked as such. A genuinely flat window is `0.0`, which is
different and is also checked. `window_seconds` must be at least one grid step.

**`feature` accepting an array.** The baseline is computed rather than stored, so it has
to be usable without being written into the panel. When an array is passed, `ICResult.feature`
is the literal string `"<array>"`. Shape must match the panel.

**`control`.** When given, report the Spearman *partial* correlation instead of the plain
one: within each symbol, rank the feature, the target and the control; regress the feature
ranks and the target ranks on the control ranks; correlate the two residual series. The
question it answers is what the feature knows that the control does not.

Consequences the tests pin, all of which follow from that definition:

- controlling a feature against itself gives exactly 0,
- controlling against a strictly increasing transform of itself also gives exactly 0,
  because ranks are preserved — this is the Phase E failure case, and it looks like
  success in any report that omits the control,
- controlling against an unrelated series leaves the IC roughly where it was.

An observation needs all three of feature, target and control finite to be used. Symbols
still need at least 3 usable observations, and `n_symbols` / `n_observations` still
report what was actually used.

## Acceptance

```acceptance
cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_research_baseline.py -q
cd backend && ./.venv/Scripts/python.exe -m pytest -q
cd backend && ./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m ruff format --check .
```

All three must exit 0. The first currently fails at import with
`ImportError: cannot import name 'trailing_volatility'`.

Report at the end:
1. The `pytest -q` summary line for the whole suite.
2. Any test whose intent you found ambiguous, and how you resolved it.
3. Anything in the oracle that looks wrong to you.
