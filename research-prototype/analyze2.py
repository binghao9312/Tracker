"""Robustness: long/short split, market baseline, barrier sizing, volatility IC."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
d = np.load(HERE / "panel.npz", allow_pickle=True)
grid, symbols = d["grid"], list(d["symbols"])
score, bias, px, tradable = d["score"], d["bias"], d["px"], d["tradable"]
T, S = score.shape
HOR = {"1m": 6, "5m": 30, "15m": 90, "60m": 360}

mk = pd.read_csv(HERE / "market.csv")
hs = mk[mk.market == "perp"].groupby("symbol").spread.median().reindex(symbols).to_numpy() * 100 / 2
cost_bps = 10.0 + 2 * np.nan_to_num(hs, nan=2.0)


def fwd(h):
    f = np.full_like(px, np.nan)
    f[:-h] = px[h:] / px[:-h] - 1.0
    return f * 1e4


def nonoverlap_t(vals, tidx, h):
    """Greedy non-overlapping-in-time sample, then cluster by timestamp."""
    o = np.argsort(tidx, kind="stable")
    vals, tidx = vals[o], tidx[o]
    keep, last = [], -10**9
    for i, t in enumerate(tidx):
        if t - last >= h:
            keep.append(i)
            if i + 1 < len(tidx) and tidx[i + 1] != t:
                last = t
    # keep all symbols sharing an accepted timestamp
    acc = set(tidx[keep])
    m = np.isin(tidx, list(acc))
    v, tt = vals[m], tidx[m]
    g = pd.DataFrame({"t": tt, "v": v}).groupby("t").v.mean()
    if len(g) < 8:
        return float(v.mean()) if len(v) else np.nan, np.nan, len(v), len(g)
    return float(v.mean()), float(g.mean() / (g.std(ddof=1) / np.sqrt(len(g)))), len(v), len(g)


tg = np.repeat(np.arange(T)[:, None], S, axis=1)
valid = np.isfinite(score) & tradable

print("=" * 78)
print("4. MARKET BASELINE  (was this window trending? would a coin-flip have won?)")
print("=" * 78)
for name, h in HOR.items():
    f = fwd(h)
    m = np.isfinite(f) & tradable
    print(f"  {name:>4}: mean fwd return of ALL symbol-obs = {f[m].mean():+7.2f} bps"
          f"   |mean| = {np.abs(f[m]).mean():6.1f} bps   n={m.sum():,}")
first = np.nanmean(px[:60], axis=0)
last = np.nanmean(px[-60:], axis=0)
drift = (last / first - 1) * 100
print(f"\n  38h buy-and-hold across {np.isfinite(drift).sum()} symbols: "
      f"median {np.nanmedian(drift):+.2f}%  mean {np.nanmean(drift):+.2f}%  "
      f"({int((drift>0).sum())} up / {int((drift<0).sum())} down)")

print("\n" + "=" * 78)
print("5. LONG vs SHORT SPLIT  (gross bps, signed by bias)")
print("=" * 78)
print(f"{'side':>6} {'n':>7} " + " ".join(f"{k:>20}" for k in HOR))
print("-" * 78)
for label, want in (("LONG", 1), ("SHORT", -1), ("both", 0)):
    sel = valid & ((bias == want) if want else (bias != 0))
    cells = []
    for name, h in HOR.items():
        f = fwd(h)
        m = sel & np.isfinite(f)
        mean, t, n, ng = nonoverlap_t((f[m] * bias[m]), tg[m], h)
        cells.append(f"{mean:>+8.1f} (t{t:>+5.2f})" if np.isfinite(t) else f"{mean:>+8.1f} (   n/a)")
    print(f"{label:>6} {int(sel.sum()):>7,} " + " ".join(f"{c:>20}" for c in cells))

print("\n  net of cost:")
print(f"{'side':>6} {'n':>7} " + " ".join(f"{k:>20}" for k in HOR))
print("-" * 78)
for label, want in (("LONG", 1), ("SHORT", -1), ("both", 0)):
    sel = valid & ((bias == want) if want else (bias != 0))
    cells = []
    for name, h in HOR.items():
        f = fwd(h)
        m = sel & np.isfinite(f)
        v = f[m] * bias[m] - cost_bps[np.nonzero(m)[1]]
        mean, t, n, ng = nonoverlap_t(v, tg[m], h)
        cells.append(f"{mean:>+8.1f} (t{t:>+5.2f})" if np.isfinite(t) else f"{mean:>+8.1f} (   n/a)")
    print(f"{label:>6} {int(sel.sum()):>7,} " + " ".join(f"{c:>20}" for c in cells))

print("\n" + "=" * 78)
print("6. WHY THE BARRIERS NEVER TRIGGER  (|forward return| distribution, %)")
print("=" * 78)
print(f"{'horizon':>8} " + " ".join(f"{f'p{q}':>8}" for q in (50, 75, 90, 95, 99))
      + f"  {'P(|r|>=1%)':>11} {'P(|r|>=2%)':>11}")
print("-" * 78)
for name, h in HOR.items():
    f = np.abs(fwd(h)) / 100
    v = f[np.isfinite(f) & tradable]
    print(f"{name:>8} " + " ".join(f"{np.percentile(v,q):>8.3f}" for q in (50, 75, 90, 95, 99))
          + f"  {100*(v>=1).mean():>10.2f}% {100*(v>=2).mean():>10.2f}%")
print("\n  TP=+2% / SL=-1% against a 60m move whose 95th percentile is "
      f"{np.percentile(np.abs(fwd(360))[np.isfinite(fwd(360))]/100, 95):.2f}%")

print("\n" + "=" * 78)
print("7. WHAT THE SCORE ACTUALLY PREDICTS  (rank IC vs forward outcome)")
print("=" * 78)
print(f"{'horizon':>8} {'IC: signed return':>20} {'IC: |return| (vol)':>21}")
print("-" * 78)
for name, h in HOR.items():
    f = fwd(h)
    m = valid & np.isfinite(f) & (bias != 0)
    ic_dir = pd.Series(score[m]).corr(pd.Series(f[m] * bias[m]), method="spearman")
    m2 = valid & np.isfinite(f)
    ic_vol = pd.Series(score[m2]).corr(pd.Series(np.abs(f[m2])), method="spearman")
    print(f"{name:>8} {ic_dir:>+20.4f} {ic_vol:>+21.4f}")
print("\n  (direction IC ~0 = no directional edge; volatility IC >0 = the score does"
      "\n   identify markets that are about to move, it just cannot say which way.)")

print("\n" + "=" * 78)
print("8. DELTA-RATIO / PRESSURE AS STANDALONE PREDICTORS")
print("=" * 78)
dr, bp5, sp5 = d["dr"], d["bp5"], d["sp5"]
print(f"{'horizon':>8} {'IC(delta_ratio)':>18} {'IC(buy-sell press)':>20}")
print("-" * 78)
for name, h in HOR.items():
    f = fwd(h)
    m = valid & np.isfinite(f)
    a = pd.Series(dr[m]).corr(pd.Series(f[m]), method="spearman")
    b = pd.Series((bp5 - sp5)[m]).corr(pd.Series(f[m]), method="spearman")
    print(f"{name:>8} {a:>+18.4f} {b:>+20.4f}")
