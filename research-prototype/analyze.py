"""Forward-return response curves + triple-barrier backtest on the rebuilt panel."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
STEP = 10
d = np.load(HERE / "panel.npz", allow_pickle=True)
grid, symbols = d["grid"], list(d["symbols"])
score, bias, px, tradable = d["score"], d["bias"], d["px"], d["tradable"]
T, S = score.shape
print(f"panel: {T:,} buckets x {S} symbols = {T*S:,} observations "
      f"({(grid[-1]-grid[0])/3600:.1f}h)\n")

# ------------------------------------------------------------------ costs
mk = pd.read_csv(HERE / "market.csv")
perp = mk[mk.market == "perp"]
half_spread_bps = perp.groupby("symbol").spread.median().reindex(symbols).to_numpy() * 100 / 2
FEE_BPS = 5.0
cost_bps = 2 * FEE_BPS + 2 * np.nan_to_num(half_spread_bps, nan=2.0)
print(f"round-trip cost: fee 10.0 bps + spread {2*np.nanmedian(half_spread_bps):.1f} bps "
      f"(median) = {np.nanmedian(cost_bps):.1f} bps\n")

# ------------------------------------------------------- score distribution
valid = np.isfinite(score)
s = score[valid]
print("=" * 74)
print("1. ACTIVITY SCORE DISTRIBUTION  (all symbol-seconds, 38.2h)")
print("=" * 74)
qs = [50, 75, 90, 95, 99, 99.9, 99.99]
print("  " + "  ".join(f"p{q}={np.percentile(s,q):6.2f}" for q in qs))
print(f"  max={s.max():.2f}   n={len(s):,}")
for thr in (25, 30, 40, 50, 60, 65, 80):
    n = int((s >= thr).sum())
    print(f"  >= {thr:>3}: {n:>8,}  ({100*n/len(s):6.3f}%)"
          + ("   <-- rearm" if thr == 65 else "   <-- ENTRY THRESHOLD" if thr == 80 else ""))

has_bias = bias != 0
print(f"\n  bias != NONE: {has_bias.sum():,} ({100*has_bias.mean():.2f}% of all obs)")
both = has_bias & valid & (score >= 80)
print(f"  score>=80 AND bias!=NONE AND tradable: {int((both & tradable).sum())}")

# -------------------------------------------------- forward return response
print("\n" + "=" * 74)
print("2. FORWARD RETURN vs SIGNAL  (signed by bias; bps; net of cost)")
print("=" * 74)
HORIZONS = {"1m": 6, "5m": 30, "15m": 90, "60m": 360}


def fwd(h: int) -> np.ndarray:
    f = np.full_like(px, np.nan)
    f[:-h] = px[h:] / px[:-h] - 1.0
    return f * 1e4  # bps


def clustered_t(vals: np.ndarray, tidx: np.ndarray, step: int) -> tuple[float, float, int]:
    """Mean + t-stat, clustering on time (handles cross-symbol correlation) and
    sampling non-overlapping timestamps (handles horizon overlap)."""
    keep = (tidx % step) == 0
    vals, tidx = vals[keep], tidx[keep]
    if len(vals) < 30:
        return np.nan, np.nan, len(vals)
    df = pd.DataFrame({"t": tidx, "v": vals}).groupby("t").v.mean()
    if len(df) < 10:
        return float(vals.mean()), np.nan, len(vals)
    return float(vals.mean()), float(df.mean() / (df.std(ddof=1) / np.sqrt(len(df)))), len(vals)


tgrid = np.repeat(np.arange(T)[:, None], S, axis=1)
print(f"\n{'threshold':>10} {'n obs':>9} " + " ".join(f"{k:>18}" for k in HORIZONS))
print(f"{'':>10} {'':>9} " + " ".join(f"{'net bps (t)':>18}" for _ in HORIZONS))
print("-" * 74)
rows = []
for thr in (0, 10, 15, 20, 25, 30):
    sel = valid & has_bias & tradable & (score >= thr)
    line, n0 = f"{'>= '+str(thr):>10}", int(sel.sum())
    cells = []
    for name, h in HORIZONS.items():
        f = fwd(h)
        m = sel & np.isfinite(f)
        signed = f[m] * bias[m]
        net = signed - cost_bps[np.nonzero(m)[1]]
        mean, t, n = clustered_t(net, tgrid[m], h)
        cells.append(f"{mean:>+8.1f} ({t:>+5.2f})" if np.isfinite(t) else f"{mean:>+8.1f} (  n/a)")
        rows.append((thr, name, mean, t, n))
    print(f"{line} {n0:>9,} " + " ".join(f"{c:>18}" for c in cells))

print("\n  (gross, before cost — is there ANY drift?)")
print(f"{'threshold':>10} {'n obs':>9} " + " ".join(f"{k:>18}" for k in HORIZONS))
print("-" * 74)
for thr in (0, 10, 15, 20, 25, 30):
    sel = valid & has_bias & tradable & (score >= thr)
    cells = []
    for name, h in HORIZONS.items():
        f = fwd(h)
        m = sel & np.isfinite(f)
        mean, t, n = clustered_t(f[m] * bias[m], tgrid[m], h)
        cells.append(f"{mean:>+8.1f} ({t:>+5.2f})" if np.isfinite(t) else f"{mean:>+8.1f} (  n/a)")
    print(f"{'>= '+str(thr):>10} {int(sel.sum()):>9,} " + " ".join(f"{c:>18}" for c in cells))

# control: does the score predict anything at all, ignoring direction?
print("\n  (control — absolute move size |fwd return|, gross bps: does score find volatility?)")
print(f"{'threshold':>10} " + " ".join(f"{k:>12}" for k in HORIZONS))
print("-" * 74)
for thr in (0, 10, 20, 30):
    sel = valid & tradable & (score >= thr)
    cells = []
    for name, h in HORIZONS.items():
        f = fwd(h)
        m = sel & np.isfinite(f)
        cells.append(f"{np.abs(f[m]).mean():>12.1f}")
    print(f"{'>= '+str(thr):>10} " + " ".join(cells))

# ------------------------------------------------------ barrier backtest
print("\n" + "=" * 74)
print("3. TRIPLE-BARRIER BACKTEST  (TP +2% / SL -1% / time stop 60m)")
print("=" * 74)
TP, SL, MAXH = 0.02, 0.01, 360
PERSIST, COOLDOWN, MAXPOS = 1, 90, 5


def backtest(entry_thr: float, rearm_thr: float, sl_first: bool,
             tp: float = TP, sl: float = SL) -> dict:
    ARMED, TRIG, COOL = 0, 1, 2
    state = np.zeros(S, np.int8)
    cool_until = np.full(S, -1)
    high_since = np.full(S, -1)
    last_bias = np.zeros(S, np.int8)
    open_pos: dict[int, tuple] = {}
    trades = []
    for t in range(T):
        for j in list(open_pos):
            e_t, e_px, side = open_pos[j]
            p = px[t, j]
            if not np.isfinite(p):
                continue
            r = (p - e_px) / e_px * side
            hit_tp, hit_sl = r >= tp, r <= -sl
            reason = None
            if sl_first:
                reason = "SL" if hit_sl else "TP" if hit_tp else None
            else:
                reason = "TP" if hit_tp else "SL" if hit_sl else None
            if reason is None and (t - e_t) >= MAXH:
                reason = "TIME"
            if reason:
                trades.append((symbols[j], side, r * 1e4 - cost_bps[j], reason, (t - e_t) * STEP))
                del open_pos[j]
                state[j], cool_until[j] = COOL, t + COOLDOWN
        sc, bi = score[t], bias[t]
        for j in range(S):
            v = sc[j]
            if not np.isfinite(v):
                continue
            if bi[j] != last_bias[j]:
                high_since[j], last_bias[j] = -1, bi[j]
            if state[j] == COOL and t >= cool_until[j]:
                state[j] = ARMED if v <= rearm_thr else TRIG
            if v <= rearm_thr and state[j] != ARMED:
                state[j], high_since[j] = ARMED, -1
                continue
            if state[j] != ARMED or v < entry_thr or bi[j] == 0 or not tradable[t, j]:
                if bi[j] == 0 or v < entry_thr:
                    high_since[j] = -1
                continue
            if high_since[j] < 0:
                high_since[j] = t
                continue
            if t - high_since[j] < PERSIST:
                continue
            state[j] = TRIG
            if j in open_pos or len(open_pos) >= MAXPOS or not np.isfinite(px[t, j]):
                continue
            open_pos[j] = (t, px[t, j], int(bi[j]))
    if not trades:
        return {"n": 0}
    df = pd.DataFrame(trades, columns=["symbol", "side", "net_bps", "reason", "secs"])
    wins = df[df.net_bps > 0]
    gl = -df[df.net_bps < 0].net_bps.sum()
    return {
        "n": len(df), "win_rate": len(wins) / len(df),
        "mean_bps": df.net_bps.mean(), "total_pct": df.net_bps.sum() / 100,
        "pf": (wins.net_bps.sum() / gl) if gl > 0 else np.inf,
        "tp": (df.reason == "TP").sum(), "sl": (df.reason == "SL").sum(),
        "time": (df.reason == "TIME").sum(),
        "median_min": df.secs.median() / 60, "nsym": df.symbol.nunique(),
        "t": df.net_bps.mean() / (df.net_bps.std(ddof=1) / np.sqrt(len(df))) if len(df) > 2 else np.nan,
    }


print(f"\nshipped config (entry=80, rearm=65): "
      f"{backtest(80, 65, False)['n']} trades  <-- threshold never reached\n")
print("threshold sweep (rearm = entry - 15), TP-first as shipped:")
hdr = f"{'entry':>6} {'trades':>7} {'sym':>4} {'win%':>6} {'mean bps':>9} {'total %':>8} {'PF':>5} {'t':>6}   TP/SL/TIME  {'med min':>7}"
print(hdr); print("-" * len(hdr))
for thr in (80, 60, 45, 35, 30, 27, 25, 22, 20, 15, 10):
    r = backtest(thr, thr - 15, False)
    if r["n"] == 0:
        print(f"{thr:>6} {'0':>7}")
        continue
    print(f"{thr:>6} {r['n']:>7} {r['nsym']:>4} {100*r['win_rate']:>6.1f} {r['mean_bps']:>+9.1f} "
          f"{r['total_pct']:>+8.2f} {r['pf']:>5.2f} {r['t']:>+6.2f}   "
          f"{r['tp']}/{r['sl']}/{r['time']}  {r['median_min']:>7.1f}")

print("\nsame, but SL checked before TP (removes the optimistic tie-break):")
print(hdr); print("-" * len(hdr))
for thr in (30, 25, 20, 15, 10):
    r = backtest(thr, thr - 15, True)
    if r["n"] == 0:
        continue
    print(f"{thr:>6} {r['n']:>7} {r['nsym']:>4} {100*r['win_rate']:>6.1f} {r['mean_bps']:>+9.1f} "
          f"{r['total_pct']:>+8.2f} {r['pf']:>5.2f} {r['t']:>+6.2f}   "
          f"{r['tp']}/{r['sl']}/{r['time']}  {r['median_min']:>7.1f}")

print("\nbarrier geometry sweep at entry=20 (TP-first):")
print(f"{'TP%':>5} {'SL%':>5} {'trades':>7} {'win%':>6} {'breakeven%':>11} {'mean bps':>9} {'total %':>8}")
print("-" * 60)
for tp, sl in ((0.02, 0.01), (0.01, 0.01), (0.01, 0.005), (0.005, 0.005), (0.02, 0.02), (0.03, 0.01)):
    r = backtest(20, 5, False, tp=tp, sl=sl)
    if r["n"] == 0:
        continue
    c = np.nanmedian(cost_bps) / 1e4
    be = (sl + c) / (tp + sl) * 100
    print(f"{100*tp:>5.1f} {100*sl:>5.1f} {r['n']:>7} {100*r['win_rate']:>6.1f} {be:>11.1f} "
          f"{r['mean_bps']:>+9.1f} {r['total_pct']:>+8.2f}")
