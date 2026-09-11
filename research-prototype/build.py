"""Reconstruct the live signal vector from persisted metrics, on a 10s grid.

Faithfulness is enforced by validating the vectorised score against the real
app.scoring.activity_score / app.trade_signal.calculate_trade_signal on a sample.

Input CSVs are produced by these three exports (adjust the window as needed);
DISTINCT ON collapses each feed to its last observation per 10s bucket:

    docker compose exec -T postgres psql -U qtrade -d qtrade -c "COPY (
    WITH rs AS (SELECT symbol FROM market_metrics GROUP BY symbol HAVING count(*)>1000)
    SELECT DISTINCT ON (symbol,exchange,market,floor(extract(epoch from timestamp)/10))
      symbol,exchange,market,
      (floor(extract(epoch from timestamp)/10)*10)::bigint AS bucket,
      price,bid_depth_2,ask_depth_2,spread
    FROM market_metrics
    WHERE symbol IN (SELECT symbol FROM rs)
      AND timestamp>='2026-09-09 10:30:00+00' AND timestamp<'2026-09-11 00:45:00+00'
    ORDER BY symbol,exchange,market,floor(extract(epoch from timestamp)/10),timestamp DESC
    ) TO STDOUT WITH CSV HEADER" > market.csv

    ... same shape for flow.csv (buy_volume_1m, sell_volume_1m, buy_volume_5m,
        sell_volume_5m, cvd_5m from flow_metrics)
    ... and deriv.csv (oi_change_5m, funding_rate from derivative_metrics,
        keyed on symbol+exchange only -- no market column)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(Path(r"C:\Users\HAO\Documents\Qtrade\backend")))

STEP = 10                     # grid seconds
FRESH_BUCKETS = 3             # ffill tolerance: 30s (live rule is 10s on received_at)
EXM = [("binance", "spot"), ("binance", "perp"), ("okx", "spot"), ("okx", "perp")]

print("loading...", flush=True)
mk = pd.read_csv(HERE / "market.csv")
fl = pd.read_csv(HERE / "flow.csv")
dv = pd.read_csv(HERE / "deriv.csv")
print(f"  market={len(mk):,} flow={len(fl):,} deriv={len(dv):,}", flush=True)

symbols = sorted(set(mk.symbol) & set(fl.symbol))
b0, b1 = int(mk.bucket.min()), int(mk.bucket.max())
grid = np.arange(b0, b1 + STEP, STEP)
print(f"  {len(symbols)} symbols, {len(grid):,} buckets "
      f"({(b1-b0)/3600:.1f}h)", flush=True)


def wide(df: pd.DataFrame, cols: list[str], keys: list[str]) -> pd.DataFrame:
    """Pivot long feed rows to <exchange>_<market>_<col> columns on the grid."""
    out = {}
    for ex, mkt in EXM:
        sel = df[(df.exchange == ex) & (df.market == mkt)] if "market" in df else df[df.exchange == ex]
        p = sel.pivot_table(index="bucket", columns="symbol", values=cols, aggfunc="last")
        for c in cols:
            out[f"{ex}_{mkt}_{c}"] = p[c].reindex(index=grid, columns=symbols)
        if "market" not in df:
            break
    return out


print("pivoting market...", flush=True)
MK = {}
for ex, mkt in EXM:
    sel = mk[(mk.exchange == ex) & (mk.market == mkt)]
    p = sel.pivot_table(index="bucket", columns="symbol",
                        values=["price", "bid_depth_2", "ask_depth_2"], aggfunc="last")
    for c in ("price", "bid_depth_2", "ask_depth_2"):
        MK[f"{ex}_{mkt}_{c}"] = p[c].reindex(index=grid, columns=symbols).ffill(limit=FRESH_BUCKETS)

print("pivoting flow...", flush=True)
FL = {}
FLOW_COLS = ["buy_volume_1m", "sell_volume_1m", "buy_volume_5m", "sell_volume_5m", "cvd_5m"]
for ex, mkt in EXM:
    sel = fl[(fl.exchange == ex) & (fl.market == mkt)]
    p = sel.pivot_table(index="bucket", columns="symbol", values=FLOW_COLS, aggfunc="last")
    for c in FLOW_COLS:
        FL[f"{ex}_{mkt}_{c}"] = p[c].reindex(index=grid, columns=symbols).ffill(limit=FRESH_BUCKETS)

print("pivoting deriv...", flush=True)
DV = {}
for ex in ("binance", "okx"):
    sel = dv[dv.exchange == ex]
    p = sel.pivot_table(index="bucket", columns="symbol",
                        values=["oi_change_5m", "funding_rate"], aggfunc="last")
    for c in ("oi_change_5m", "funding_rate"):
        DV[f"{ex}_{c}"] = p[c].reindex(index=grid, columns=symbols).ffill(limit=6)

# ---------------------------------------------------------------- aggregation
# Mirrors runtime._persist_flow: an exchange contributes volume only when that
# exchange's book was fresh (i.e. a market row exists), and depth is the sum of
# the fresh books' 2% depth for that market.
Z = pd.DataFrame(0.0, index=grid, columns=symbols)


def agg_market(mkt: str) -> dict[str, pd.DataFrame]:
    bv1 = Z.copy(); sv1 = Z.copy(); bv5 = Z.copy(); sv5 = Z.copy()
    cvd5 = Z.copy(); dask = Z.copy(); dbid = Z.copy()
    any_book = pd.DataFrame(False, index=grid, columns=symbols)
    for ex in ("binance", "okx"):
        fresh = MK[f"{ex}_{mkt}_ask_depth_2"].notna()
        any_book |= fresh
        dask += MK[f"{ex}_{mkt}_ask_depth_2"].fillna(0.0)
        dbid += MK[f"{ex}_{mkt}_bid_depth_2"].fillna(0.0)
        for src, dst in (("buy_volume_1m", bv1), ("sell_volume_1m", sv1),
                         ("buy_volume_5m", bv5), ("sell_volume_5m", sv5),
                         ("cvd_5m", cvd5)):
            dst += FL[f"{ex}_{mkt}_{src}"].where(fresh).fillna(0.0)

    def press(vol, depth):
        return (vol / depth.where(depth > 0)).where(any_book)

    tot5 = bv5 + sv5
    return {
        "buy_p1": press(bv1, dask), "sell_p1": press(sv1, dbid),
        "buy_p5": press(bv5, dask), "sell_p5": press(sv5, dbid),
        "bv5": bv5.where(any_book), "sv5": sv5.where(any_book),
        "cvd5": cvd5.where(any_book),
        "cvd_ratio": ((bv5 - sv5) / tot5.where(tot5 > 0)).fillna(0.0).where(any_book),
    }


SPOT, PERP = agg_market("spot"), agg_market("perp")


def per_exchange_direction(ex: str) -> pd.DataFrame:
    """scoring._exchange_direction over that exchange's own spot+perp books."""
    PRESSURE_T, CVD_T = 2.0, 0.0
    dirs = []
    for mkt in ("spot", "perp"):
        d_ask = MK[f"{ex}_{mkt}_ask_depth_2"]
        d_bid = MK[f"{ex}_{mkt}_bid_depth_2"]
        bp = (FL[f"{ex}_{mkt}_buy_volume_5m"] / d_ask.where(d_ask > 0)).fillna(0.0)
        sp = (FL[f"{ex}_{mkt}_sell_volume_5m"] / d_bid.where(d_bid > 0)).fillna(0.0)
        cvd = FL[f"{ex}_{mkt}_cvd_5m"]
        buy = (bp >= PRESSURE_T) & (bp > sp) & (cvd > CVD_T)
        sell = (sp >= PRESSURE_T) & (sp > bp) & (cvd < -CVD_T)
        dirs.append(np.where(buy, 1, np.where(sell, -1, 0)))
    a, b = dirs
    # {BUY, SELL, NONE} minus NONE; unique survivor wins, else NONE
    both = np.where(a == 0, b, np.where(b == 0, a, np.where(a == b, a, 0)))
    return pd.DataFrame(both, index=grid, columns=symbols)


DIR_B, DIR_O = per_exchange_direction("binance"), per_exchange_direction("okx")
# cross_exchange_state == CONFIRMED requires 2 signals, both active, all equal
CONFIRMED = (DIR_B != 0) & (DIR_O != 0) & (DIR_B == DIR_O)

# ------------------------------------------------------------------ the score
def market_pressure(p1: pd.DataFrame, p5: pd.DataFrame) -> pd.DataFrame:
    """scoring._market_pressure: 0.45/0.55 blend of min(|v|/3, 1) over available."""
    c1, c5 = (p1.abs() / 3).clip(upper=1.0), (p5.abs() / 3).clip(upper=1.0)
    num = c1.fillna(0) * 0.45 + c5.fillna(0) * 0.55
    den = p1.notna() * 0.45 + p5.notna() * 0.55
    return (num / den.where(den > 0))


def pressure_magnitude(buy: pd.DataFrame, sell: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(np.fmax(buy.to_numpy(), sell.to_numpy()), index=grid, columns=symbols)


def available_mean(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    num = a.fillna(0) + b.fillna(0)
    den = a.notna().astype(float) + b.notna().astype(float)
    return (num / den.where(den > 0)).fillna(0.0)


spot_mp = market_pressure(pressure_magnitude(SPOT["buy_p1"], SPOT["sell_p1"]),
                          pressure_magnitude(SPOT["buy_p5"], SPOT["sell_p5"]))
perp_mp = market_pressure(pressure_magnitude(PERP["buy_p1"], PERP["sell_p1"]),
                          pressure_magnitude(PERP["buy_p5"], PERP["sell_p5"]))
pressure_component = available_mean(spot_mp, perp_mp)
flow_component = available_mean(SPOT["cvd_ratio"].abs().clip(upper=1.0),
                                PERP["cvd_ratio"].abs().clip(upper=1.0))

# largest-absolute OI change across exchanges (runtime._largest_absolute)
oi_b, oi_o = DV["binance_oi_change_5m"], DV["okx_oi_change_5m"]
oi = oi_b.where(oi_b.abs().fillna(-1) >= oi_o.abs().fillna(-1), oi_o)
fund = pd.concat([DV["binance_funding_rate"], DV["okx_funding_rate"]]).groupby(level=0).mean()
fund = fund.reindex(index=grid, columns=symbols)

SCORE = (50 * pressure_component
         + 30 * flow_component
         + 10 * oi.abs().clip(upper=1.0).fillna(0.0)
         + 5 * (fund.abs() * 1000).clip(upper=1.0).fillna(0.0)
         + 5 * CONFIRMED).clip(upper=100).round(2)

# ------------------------------------------------------------------- the bias
bp5 = pd.DataFrame(np.fmax(SPOT["buy_p5"].to_numpy(), PERP["buy_p5"].to_numpy()),
                   index=grid, columns=symbols).fillna(0.0)
sp5 = pd.DataFrame(np.fmax(SPOT["sell_p5"].to_numpy(), PERP["sell_p5"].to_numpy()),
                   index=grid, columns=symbols).fillna(0.0)
bv = SPOT["bv5"].fillna(0.0) + PERP["bv5"].fillna(0.0)
sv = SPOT["sv5"].fillna(0.0) + PERP["sv5"].fillna(0.0)
tot = bv + sv
DR = ((bv - sv) / tot.where(tot > 0)).fillna(0.0)
LONG = (bp5 >= 2.0) & (bp5 >= sp5 * 1.5) & (DR >= 0.15)
SHORT = (sp5 >= 2.0) & (sp5 >= bp5 * 1.5) & (DR <= -0.15)
BIAS = pd.DataFrame(np.where(LONG, 1, np.where(SHORT, -1, 0)), index=grid, columns=symbols)

# ------------------------------------------------------- execution / ref price
# paper_trading._execution_book prefers binance perp, then okx perp.
PX = MK["binance_perp_price"].where(MK["binance_perp_price"].notna(), MK["okx_perp_price"])
VENUE = np.where(MK["binance_perp_price"].notna(), "binance",
                 np.where(MK["okx_perp_price"].notna(), "okx", ""))
TRADABLE = pd.DataFrame(VENUE != "", index=grid, columns=symbols)

# ------------------------------------------------------------------- validate
print("\nvalidating against app.scoring / app.trade_signal ...", flush=True)
from app.scoring import activity_score as real_score          # noqa: E402
from app.trade_signal import calculate_trade_signal           # noqa: E402

def _max(a: float | None, b: float | None) -> float | None:
    """runtime._pressure_magnitude: max over the available values."""
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


rng = np.random.default_rng(0)
rows = [(int(rng.integers(0, len(grid))), int(rng.integers(0, len(symbols)))) for _ in range(4000)]
sd = se = bd = 0
for i, j in rows:
    if not np.isfinite(SCORE.iat[i, j]):
        continue

    def g(d, k, _i=None, _j=None):
        v = d[k].iat[i, j]
        return None if pd.isna(v) else float(v)

    got = real_score(
        oi_change=None if pd.isna(oi.iat[i, j]) else float(oi.iat[i, j]),
        funding=None if pd.isna(fund.iat[i, j]) else float(fund.iat[i, j]),
        confirmed=bool(CONFIRMED.iat[i, j]),
        spot_pressure_1m=_max(g(SPOT, "buy_p1"), g(SPOT, "sell_p1")),
        spot_pressure_5m=_max(g(SPOT, "buy_p5"), g(SPOT, "sell_p5")),
        spot_cvd_ratio=g(SPOT, "cvd_ratio"),
        perp_pressure_1m=_max(g(PERP, "buy_p1"), g(PERP, "sell_p1")),
        perp_pressure_5m=_max(g(PERP, "buy_p5"), g(PERP, "sell_p5")),
        perp_cvd_ratio=g(PERP, "cvd_ratio"),
    )
    if abs(got - float(SCORE.iat[i, j])) > 0.02:
        sd += 1
    se += 1
    detail = {
        "spot": {"buy_pressure_5m": g(SPOT, "buy_p5"), "sell_pressure_5m": g(SPOT, "sell_p5"),
                 "buy_volume_5m": g(SPOT, "bv5"), "sell_volume_5m": g(SPOT, "sv5")},
        "perp": {"buy_pressure_5m": g(PERP, "buy_p5"), "sell_pressure_5m": g(PERP, "sell_p5"),
                 "buy_volume_5m": g(PERP, "bv5"), "sell_volume_5m": g(PERP, "sv5")},
    }
    want = {"LONG": 1, "SHORT": -1, "NONE": 0}[calculate_trade_signal(detail).bias.value]
    if want != int(BIAS.iat[i, j]):
        bd += 1
print(f"  checked {se} rows: score mismatches={sd}  bias mismatches={bd}")

out = HERE / "panel.npz"
np.savez_compressed(
    out,
    grid=grid, symbols=np.array(symbols),
    score=SCORE.to_numpy(np.float32), bias=BIAS.to_numpy(np.int8),
    px=PX.to_numpy(np.float64), tradable=TRADABLE.to_numpy(bool),
    confirmed=CONFIRMED.to_numpy(bool),
    spot_px=MK["binance_spot_price"].to_numpy(np.float64),
    dr=DR.to_numpy(np.float32), bp5=bp5.to_numpy(np.float32), sp5=sp5.to_numpy(np.float32),
    oi=oi.to_numpy(np.float32), fund=fund.to_numpy(np.float32),
    pc=pressure_component.to_numpy(np.float32), fc=flow_component.to_numpy(np.float32),
)
print(f"\nwrote {out} ({out.stat().st_size/1e6:.1f} MB)")
