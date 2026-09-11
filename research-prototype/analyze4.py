"""Resolve the volatility question WITHIN symbol (pooled IC is confounded by
cross-sectional vol differences), and re-run the alpha test on a common block grid."""
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
valid = np.isfinite(score) & tradable


def fwd(h):
    f = np.full_like(px, np.nan)
    f[:-h] = px[h:] / px[:-h] - 1.0
    return f * 1e4


print("=" * 84)
print("12. IS THE SCORE A VOLATILITY DETECTOR?  (within-symbol, so vol differences")
print("    between coins cannot create a spurious relationship)")
print("=" * 84)
print(f"{'horizon':>8} {'IC (median over symbols)':>26} {'symbols +ve':>12}   "
      f"{'|ret| at score p50':>19} {'p90':>9} {'p99':>9}")
print("-" * 84)
for name, h in HOR.items():
    f = np.abs(fwd(h))
    ics, lo, mid, hi = [], [], [], []
    for j in range(S):
        m = valid[:, j] & np.isfinite(f[:, j])
        if m.sum() < 500:
            continue
        sc, fv = score[m, j], f[m, j]
        ics.append(np.corrcoef(pd.Series(sc).rank(), pd.Series(fv).rank())[0, 1])
        q50, q90, q99 = np.percentile(sc, [50, 90, 99])
        # normalise by the symbol's own median |ret| so symbols are comparable
        base = np.median(fv) or 1.0
        lo.append(fv[sc <= q50].mean() / base)
        mid.append(fv[(sc >= q90)].mean() / base)
        hi.append(fv[(sc >= q99)].mean() / base)
    ics = np.array(ics)
    print(f"{name:>8} {np.median(ics):>+26.4f} {f'{(ics>0).sum()}/{len(ics)}':>12}   "
          f"{np.median(lo):>18.2f}x {np.median(mid):>8.2f}x {np.median(hi):>8.2f}x")
print("\n  (|ret| shown as a multiple of that symbol's own median |ret|, so 1.00x = typical.)")

print("\n" + "=" * 84)
print("13. DIRECTIONAL ALPHA on a COMMON block grid (all subsets share the same")
print("    non-overlapping blocks, so LONG/SHORT/both are directly comparable)")
print("=" * 84)
tg = np.repeat(np.arange(T)[:, None], S, axis=1)


def demean(f):
    mu = np.nanmean(np.where(tradable, f, np.nan), axis=0, keepdims=True)
    return f - mu


print(f"{'side':>6} {'signals':>8} " + " ".join(f"{k:>20}" for k in HOR))
print("-" * 84)
res = {}
for label, want in (("LONG", 1), ("SHORT", -1), ("both", 0)):
    sel = valid & ((bias == want) if want else (bias != 0))
    cells = []
    for name, h in HOR.items():
        f = demean(fwd(h))
        m = sel & np.isfinite(f)
        blocks = tg[m] // h                       # fixed, shared blocks
        g = pd.DataFrame({"b": blocks, "v": (f * bias)[m]}).groupby("b").v.mean()
        n_all = T // h
        a = g.to_numpy()
        t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 2 else np.nan
        cells.append(f"{a.mean():>+8.1f} (t{t:>+5.2f},k{len(a):>3})")
        res[(label, name)] = (a.mean(), t, len(a))
    print(f"{label:>6} {int(sel.sum()):>8,} " + " ".join(f"{c:>20}" for c in cells))

print("\n  Cross-check: 'both' must sit between LONG and SHORT on the same grid.")
for name in HOR:
    lo, sh, bo = res[("LONG", name)][0], res[("SHORT", name)][0], res[("both", name)][0]
    ok = min(lo, sh) - 1e-9 <= bo <= max(lo, sh) + 1e-9
    print(f"    {name:>4}: LONG {lo:+7.1f}  SHORT {sh:+7.1f}  both {bo:+7.1f}   "
          f"{'consistent' if ok else '** INCONSISTENT **'}")

print("\n" + "=" * 84)
print("14. BREAKEVEN GAP  (what the signal delivers vs what the barriers require)")
print("=" * 84)
mk = pd.read_csv(HERE / "market.csv")
hs = mk[mk.market == "perp"].groupby("symbol").spread.median().reindex(symbols).to_numpy() * 100 / 2
c = 10.0 + 2 * np.nanmedian(hs)
for tp, sl in ((2.0, 1.0), (1.0, 0.5)):
    be = (sl * 100 + c) / ((tp + sl) * 100) * 100
    rw = sl / (tp + sl) * 100
    print(f"  TP {tp}% / SL {sl}%:  random-walk win rate {rw:.1f}%  ->  "
          f"breakeven {be:.1f}%   (need +{be-rw:.1f} pp of directional skill)")
print(f"\n  measured timing alpha at 60m (the actual holding period): "
      f"{res[('both','60m')][0]:+.1f} bps, t={res[('both','60m')][1]:+.2f}")
print(f"  round-trip cost:                                          {c:.1f} bps")
print(f"  => net expectancy per trade:                              "
      f"{res[('both','60m')][0]-c:+.1f} bps")
