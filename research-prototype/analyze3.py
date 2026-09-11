"""Isolate timing alpha from market beta: symbol-demeaned forward returns."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
d = np.load(HERE / "panel.npz", allow_pickle=True)
symbols = list(d["symbols"])
score, bias, px, tradable = d["score"], d["bias"], d["px"], d["tradable"]
T, S = score.shape
HOR = {"1m": 6, "5m": 30, "15m": 90, "60m": 360}

mk = pd.read_csv(HERE / "market.csv")
hs = mk[mk.market == "perp"].groupby("symbol").spread.median().reindex(symbols).to_numpy() * 100 / 2
cost_bps = 10.0 + 2 * np.nan_to_num(hs, nan=2.0)
valid = np.isfinite(score) & tradable
tg = np.repeat(np.arange(T)[:, None], S, axis=1)
sg = np.repeat(np.arange(S)[None, :], T, axis=0)


def fwd(h):
    f = np.full_like(px, np.nan)
    f[:-h] = px[h:] / px[:-h] - 1.0
    return f * 1e4


def demean(f):
    """Subtract each symbol's own mean forward return over the window.
    Removes the -6% market drift and any per-symbol trend, leaving timing skill."""
    mu = np.nanmean(np.where(tradable, f, np.nan), axis=0, keepdims=True)
    return f - mu


def cluster_stats(vals, tidx, h):
    """Cluster by timestamp, then thin to non-overlapping clusters."""
    g = pd.DataFrame({"t": tidx, "v": vals}).groupby("t").v.mean().sort_index()
    keep, last = [], -10**9
    for t, v in g.items():
        if t - last >= h:
            keep.append(v)
            last = t
    a = np.asarray(keep, float)
    if len(a) < 8:
        return float(vals.mean()), np.nan, len(vals), len(a)
    return float(a.mean()), float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))), len(vals), len(a)


print("=" * 86)
print("9. TIMING ALPHA  (forward return demeaned per symbol -> market/trend drift removed)")
print("=" * 86)
print(f"{'side':>6} {'signals':>8} " + " ".join(f"{k:>18}" for k in HOR))
print(f"{'':>6} {'':>8} " + " ".join(f"{'alpha bps (t, k)':>18}" for _ in HOR))
print("-" * 86)
for label, want in (("LONG", 1), ("SHORT", -1), ("both", 0)):
    sel = valid & ((bias == want) if want else (bias != 0))
    cells = []
    for name, h in HOR.items():
        f = demean(fwd(h))
        m = sel & np.isfinite(f)
        mean, t, n, k = cluster_stats(f[m] * bias[m], tg[m], h)
        cells.append(f"{mean:>+7.1f} ({t:>+5.2f},{k:>4})" if np.isfinite(t)
                     else f"{mean:>+7.1f} (  n/a,{k:>4})")
    print(f"{label:>6} {int(sel.sum()):>8,} " + " ".join(f"{c:>18}" for c in cells))
print("\n  k = number of independent (non-overlapping) time clusters behind the t-stat.")
print("  |t| > 2 with k > 30 would be the minimum bar for 'there is something here'.")

print("\n" + "=" * 86)
print("10. PASSIVE BENCHMARK  (what would holding the same side, same symbol, at a")
print("    RANDOM time have earned? -> is the signal's timing better than nothing?)")
print("=" * 86)
rng = np.random.default_rng(7)
print(f"{'horizon':>8} {'SHORT signal':>14} {'SHORT random':>14} {'diff':>9}   "
      f"{'LONG signal':>13} {'LONG random':>13} {'diff':>9}")
print("-" * 86)
for name, h in HOR.items():
    f = fwd(h)
    out = []
    for want in (-1, 1):
        sel = valid & (bias == want) & np.isfinite(f)
        sig = (f[sel] * want).mean()
        # same symbols, same count, uniformly random timestamps
        tot = 0.0, 0
        acc = []
        for j in np.unique(sg[sel]):
            n = int((sg[sel] == j).sum())
            col = f[:, j]
            ok = np.nonzero(np.isfinite(col) & tradable[:, j])[0]
            if len(ok) == 0:
                continue
            pick = rng.choice(ok, size=min(n * 20, 20000), replace=True)
            acc.append(np.repeat((col[pick] * want).mean(), n))
        rnd = np.concatenate(acc).mean() if acc else np.nan
        out += [sig, rnd, sig - rnd]
    print(f"{name:>8} {out[0]:>+14.1f} {out[1]:>+14.1f} {out[2]:>+9.1f}   "
          f"{out[3]:>+13.1f} {out[4]:>+13.1f} {out[5]:>+9.1f}")
print("\n  (bps, gross. 'diff' is the signal's timing contribution over picking a")
print("   random moment in the same symbol on the same side.)")

print("\n" + "=" * 86)
print("11. RANK IC  (Spearman, pooled)")
print("=" * 86)
dr, bp5, sp5 = d["dr"], d["bp5"], d["sp5"]


def ic(a, b):
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 100:
        return np.nan
    ra = pd.Series(a[m]).rank().to_numpy()
    rb = pd.Series(b[m]).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])


print(f"{'horizon':>8} {'score->signed ret':>19} {'score->|ret| (vol)':>20} "
      f"{'delta_ratio->ret':>18} {'press diff->ret':>17}")
print("-" * 86)
for name, h in HOR.items():
    f = fwd(h)
    fd = demean(f)
    m = valid & np.isfinite(f) & (bias != 0)
    m2 = valid & np.isfinite(f)
    print(f"{name:>8} {ic(score[m], (fd*bias)[m]):>+19.4f} {ic(score[m2], np.abs(f[m2])):>+20.4f} "
          f"{ic(dr[m2], fd[m2]):>+18.4f} {ic((bp5-sp5)[m2], fd[m2]):>+17.4f}")
print("\n  A tradable short-horizon signal typically needs |IC| >= 0.02-0.03.")
