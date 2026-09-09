import { useEffect, useMemo, useState } from "react";
import { MetricChart, type ChartPoint } from "./MetricChart";

type ScannerRow = {
  symbol: string;
  market_cap_rank: number;
  price: number | null;
  liquidity_fragility: number | null;
  activity_score: number | null;
  buy_pressure_1m: number | null;
  buy_pressure_5m: number | null;
  sell_pressure_1m: number | null;
  sell_pressure_5m: number | null;
  spot_cvd_5m: number | null;
  perp_cvd_5m: number | null;
  oi_change_5m: number | null;
  funding: number | null;
  move_type: string | null;
  cross_exchange_state: string | null;
};
type Detail = Record<string, unknown>;
type SortKey = keyof ScannerRow;
type PaperTrade = Record<string, unknown> & { id: number; symbol: string; side: string; status: string; opened_at: string; closed_at: string | null; entry_price: number; exit_price: number | null; net_pnl: number | null; return_pct: number | null; max_favorable_excursion_pct: number; max_adverse_excursion_pct: number; exit_reason: string | null };
type PaperStats = Record<string, unknown> & { total_trades: number; open_trades: number; win_rate: number; net_pnl: number; profit_factor: number | null };

const number = (value: number | null | undefined, digits = 2) => value == null ? "—" : value.toLocaleString(undefined, { maximumFractionDigits: digits });
const percent = (value: number | null | undefined) => value == null ? "—" : `${value >= 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;
const socketUrl = (path: string) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;

type SymbolHistory = { market?: { timestamp: string | number; price: number | null }[] };

const scannerColumns = [
  "symbol", "market_cap_rank", "price", "liquidity_fragility", "activity_score",
  "buy_pressure_1m", "buy_pressure_5m", "sell_pressure_1m", "sell_pressure_5m",
  "spot_cvd_5m", "perp_cvd_5m", "oi_change_5m", "funding", "move_type", "cross_exchange_state",
] as const satisfies readonly SortKey[];

const marketChartPoints = (history: SymbolHistory): ChartPoint[] => {
  const raw = (history.market ?? []).flatMap(({ timestamp, price }) => {
    const milliseconds = typeof timestamp === "number" ? timestamp : Date.parse(timestamp);
    return typeof price === "number" && Number.isFinite(price) && Number.isFinite(milliseconds)
      ? [{ timestamp: milliseconds, price }]
      : [];
  });
  raw.sort((a, b) => a.timestamp - b.timestamp);
  const deduped: ChartPoint[] = [];
  let lastSec = -Infinity;
  for (const pt of raw) {
    const sec = Math.floor(pt.timestamp / 1000);
    if (sec > lastSec) {
      deduped.push(pt);
      lastSec = sec;
    }
  }
  return deduped;
};


export function App() {
  const [mode, setMode] = useState<"SCANNER" | "PAPER">("SCANNER");
  const [rows, setRows] = useState<ScannerRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [historyPoints, setHistoryPoints] = useState<ChartPoint[]>([]);
  const [sort, setSort] = useState<{ key: SortKey; descending: boolean }>({ key: "activity_score", descending: true });
  const [connection, setConnection] = useState("CONNECTING");
  const [trades, setTrades] = useState<PaperTrade[]>([]);
  const [positions, setPositions] = useState<PaperTrade[]>([]);
  const [stats, setStats] = useState<PaperStats | null>(null);
  const [replay, setReplay] = useState<{ trade: PaperTrade; entry_snapshot: Detail; exit_snapshot: Detail | null; history: { market: ChartPoint[] } } | null>(null);

  useEffect(() => {
    void fetch("/api/scanner").then(response => response.json()).then(setRows).catch(() => setConnection("OFFLINE"));
    const socket = new WebSocket(socketUrl("/ws/scanner"));
    socket.onopen = () => setConnection("CONNECTED");
    socket.onclose = () => setConnection("RECONNECTING");
    socket.onmessage = ({ data }) => {
      const message = JSON.parse(data) as { type: string; data: ScannerRow | ScannerRow[] };
      if (message.type !== "scanner") return;
      if (Array.isArray(message.data)) {
        setRows(message.data);
        return;
      }
      const update = message.data as ScannerRow;
      setRows(current => [...current.filter(row => row.symbol !== update.symbol), update]);
    };
    return () => socket.close();
  }, []);

  useEffect(() => {
    if (!selected) return;
    let current = true;
    setDetail(null);
    setHistoryPoints([]);
    void fetch(`/api/symbol/${selected}`).then(response => response.ok ? response.json() as Promise<Detail> : null).then(value => {
      if (current) setDetail(value);
    }).catch(() => {
      if (current) setDetail(null);
    });
    void fetch(`/api/symbol/${selected}/history`).then(response => response.ok ? response.json() as Promise<SymbolHistory> : null).then(value => {
      if (current) setHistoryPoints(value ? marketChartPoints(value) : []);
    }).catch(() => {
      if (current) setHistoryPoints([]);
    });
    return () => { current = false; };
  }, [selected]);

  useEffect(() => {
    if (!selected || mode !== "SCANNER") return;
    const socket = new WebSocket(socketUrl(`/ws/symbol/${selected}`));
    socket.onmessage = ({ data }) => { const message = JSON.parse(data) as { type: string; data: Detail }; if (message.type === "symbol") setDetail(message.data); };
    return () => socket.close();
  }, [selected, mode]);

  useEffect(() => {
    if (mode !== "PAPER") return;
    const refresh = () => {
      void fetch("/api/paper/trades?limit=250").then(response => response.json()).then(setTrades);
      void fetch("/api/paper/positions").then(response => response.json()).then(setPositions);
      void fetch("/api/paper/stats").then(response => response.json()).then(setStats);
    };
    refresh();
    const socket = new WebSocket(socketUrl("/ws/paper"));
    socket.onmessage = refresh;
    return () => socket.close();
  }, [mode]);

  const ordered = useMemo(() => [...rows].sort((left, right) => {
    const a = left[sort.key] ?? (sort.descending ? -Infinity : Infinity);
    const b = right[sort.key] ?? (sort.descending ? -Infinity : Infinity);
    return (a < b ? -1 : a > b ? 1 : 0) * (sort.descending ? -1 : 1);
  }), [rows, sort]);
  const chooseSort = (key: SortKey) => setSort(current => ({ key, descending: current.key === key ? !current.descending : key !== "symbol" }));
  const loadReplay = (trade: PaperTrade) => void fetch(`/api/paper/trades/${trade.id}`).then(response => response.json()).then(setReplay);
  const currentPrices = useMemo(() => Object.fromEntries(rows.filter(row => row.price != null).map(row => [row.symbol, row.price!])), [rows]);
  return <main className="terminal">
    <header className="toolbar"><strong>QTRADE / CEX LIQUIDITY & FLOW</strong><nav><button className={mode === "SCANNER" ? "active" : ""} onClick={() => setMode("SCANNER")}>SCANNER</button><button className={mode === "PAPER" ? "active" : ""} onClick={() => setMode("PAPER")}>PAPER</button></nav><span>BINANCE <i className="online"/> OKX <i className="online"/></span><span className="connection">{connection}</span></header>
    {mode === "SCANNER" ? <><section className="scanner"><table><thead><tr>{scannerColumns.map(key => <th key={key} onClick={() => chooseSort(key)}>{key.replaceAll("_", " ")}{sort.key === key ? sort.descending ? " ↓" : " ↑" : ""}</th>)}</tr></thead><tbody>{ordered.map(row => <tr key={row.symbol} className={row.symbol === selected ? "selected" : ""} onClick={() => setSelected(row.symbol)}>{scannerColumns.map(key => <td key={key}>{typeof row[key] === "number" ? number(row[key], 4) : row[key] ?? "—"}</td>)}</tr>)}</tbody></table></section><section className="detail"><DetailPane selected={selected} detail={detail}/><MetricChart data={historyPoints}/></section></> : <PaperPage stats={stats} positions={positions} trades={trades} replay={replay} currentPrices={currentPrices} onReplay={loadReplay} onCloseReplay={() => setReplay(null)}/>}
  </main>;
}
function DetailPane({ selected, detail }: { selected: string | null; detail: Detail | null }) {
  if (!selected || !detail) {
    return <div><h2>{selected ?? "SELECT SYMBOL"}</h2><p>Choose a scanner row for exchange-normalized liquidity, flow, and derivatives metrics.</p></div>;
  }
  const spot = (detail.spot ?? {}) as Detail;
  const perp = (detail.perp ?? {}) as Detail;
  const liq = (detail.liquidity ?? {}) as Record<string, number | null | undefined>;
  const oiEx = (detail.oi_change_by_exchange ?? {}) as Record<string, Record<string, number | null>>;
  const moveEx = (detail.move_type_by_exchange ?? {}) as Record<string, string>;
  const dirEx = (detail.exchange_directions ?? {}) as Record<string, string>;
  return <div className="detail-content">
    <div className="detail-header"><h2>{selected} · {number(detail.price as number, 2)}</h2><div className="detail-tags"><span className="tag">{String(detail.move_type ?? "NEUTRAL")}</span><span className={`tag ${detail.cross_exchange_state === "CONFIRMED" ? "positive" : "negative"}`}>{String(detail.cross_exchange_state ?? "—")}</span></div></div>
    <dl>
      <div><dt>Activity Score</dt><dd>{number(detail.activity_score as number, 2)}</dd></div>
      <div><dt>Liquidity Fragility</dt><dd>{number(detail.liquidity_fragility as number, 2)}</dd></div>
      <div><dt>Funding Rate</dt><dd>{percent(detail.funding as number)}</dd></div>
      <div><dt>OI Δ 5m</dt><dd>{percent(detail.oi_change_5m as number)}</dd></div>
      <div><dt>OI Δ 15m</dt><dd>{percent(detail.oi_change_15m as number)}</dd></div>
      <div><dt>OI Δ 1h</dt><dd>{percent(detail.oi_change_1h as number)}</dd></div>
    </dl>
    <div className="detail-subtable"><small>ORDER BOOK DEPTH & IMPACT</small><table><thead><tr><th>Metric</th><th>Bid / Down</th><th>Ask / Up</th><th>Context</th></tr></thead><tbody>
      <tr><td>Depth ±2%</td><td>{number(liq.bid_depth_2 as number, 0)} USDT</td><td>{number(liq.ask_depth_2 as number, 0)} USDT</td><td>Spread {number(liq.spread_percent as number, 2)}%</td></tr>
      <tr><td>$10K Impact</td><td>{percent(liq.sell_impact_10k as number)}</td><td>{percent(liq.buy_impact_10k as number)}</td><td>$50K {percent(liq.buy_impact_50k as number)}</td></tr>
      <tr><td>Cap to Move 1%</td><td>{number(liq.capital_to_move_down_1pct as number, 0)} USDT</td><td>{number(liq.capital_to_move_up_1pct as number, 0)} USDT</td><td>OBI {number(liq.order_book_imbalance as number, 3)}</td></tr>
    </tbody></table></div>
    <div className="detail-subtable"><small>SPOT VS PERP FLOW</small><table><thead><tr><th>Market</th><th>Buy Press (1m/5m)</th><th>Sell Press (1m/5m)</th><th>5m CVD</th></tr></thead><tbody>
      <tr><td>Spot</td><td>{number(spot.buy_pressure_1m as number)} / {number(spot.buy_pressure_5m as number)}</td><td>{number(spot.sell_pressure_1m as number)} / {number(spot.sell_pressure_5m as number)}</td><td className={(spot.cvd_5m as number ?? 0) >= 0 ? "positive" : "negative"}>{number(spot.cvd_5m as number, 0)}</td></tr>
      <tr><td>Perp</td><td>{number(perp.buy_pressure_1m as number)} / {number(perp.buy_pressure_5m as number)}</td><td>{number(perp.sell_pressure_1m as number)} / {number(perp.sell_pressure_5m as number)}</td><td className={(perp.cvd_5m as number ?? 0) >= 0 ? "positive" : "negative"}>{number(perp.cvd_5m as number, 0)}</td></tr>
    </tbody></table></div>
    <div className="detail-subtable"><small>CROSS EXCHANGE EVIDENCE</small><table><thead><tr><th>Exchange</th><th>Direction</th><th>Move Type</th><th>OI Δ 5m</th></tr></thead><tbody>
      {["binance", "okx"].map(ex => <tr key={ex}><td>{ex.toUpperCase()}</td><td className={dirEx[ex] === "BUY" ? "positive" : dirEx[ex] === "SELL" ? "negative" : ""}>{dirEx[ex] ?? "—"}</td><td>{moveEx[ex] ?? "—"}</td><td>{percent(oiEx[ex]?.["5m"])}</td></tr>)}
    </tbody></table></div>
  </div>;
}


function PaperPage({ stats, positions, trades, replay, currentPrices, onReplay, onCloseReplay }: { stats: PaperStats | null; positions: PaperTrade[]; trades: PaperTrade[]; replay: { trade: PaperTrade; entry_snapshot: Detail; exit_snapshot: Detail | null; history: { market: ChartPoint[] } } | null; currentPrices: Record<string, number>; onReplay: (trade: PaperTrade) => void; onCloseReplay: () => void }) {
  if (replay) return <section className="replay"><button onClick={onCloseReplay}>← PAPER TRADES</button><h2>TRADE REPLAY / {replay.trade.symbol} {replay.trade.side}</h2><p>Entry {new Date(replay.trade.opened_at).toLocaleString()} · Price {number(replay.trade.entry_price)} · Activity {number(replay.trade.entry_activity_score as number)} · Fragility {number(replay.trade.entry_liquidity_fragility as number)}</p><p>Exit {replay.trade.exit_reason ?? "OPEN"} · Price {number(replay.trade.exit_price)} · Return {percent(replay.trade.return_pct)} · PnL {number(replay.trade.net_pnl)} · MFE {percent(replay.trade.max_favorable_excursion_pct)} · MAE {percent(replay.trade.max_adverse_excursion_pct)}</p><MetricChart data={replay.history.market ?? []} markers={[{ timestamp: replay.trade.opened_at, price: replay.trade.entry_price, label: "ENTRY", color: "#42d392", position: "belowBar" }, ...(replay.trade.exit_price !== null && replay.trade.closed_at ? [{ timestamp: replay.trade.closed_at, price: replay.trade.exit_price, label: "EXIT", color: "#ff6b6b", position: "aboveBar" } as const] : [])]}/><SignalMetrics snapshot={replay.entry_snapshot}/></section>;
  return <section className="paper"><div className="stats">{[["TOTAL TRADES", stats?.total_trades], ["WIN RATE", stats ? percent(stats.win_rate) : null], ["NET PNL", stats?.net_pnl == null ? null : number(stats.net_pnl)], ["PROFIT FACTOR", stats?.profit_factor], ["OPEN POSITIONS", stats?.open_trades]].map(([label, value]) => <div key={String(label)}><small>{label}</small><strong>{typeof value === "number" ? number(value) : value ?? "—"}</strong></div>)}</div><h2>OPEN POSITIONS</h2><TradeTable trades={positions} currentPrices={currentPrices}/><h2>TRADE HISTORY</h2><TradeTable trades={trades} currentPrices={currentPrices} onReplay={onReplay}/><PerformanceBreakdowns breakdowns={stats?.breakdowns as Record<string, Record<string, BreakdownSummary>> | undefined}/></section>;
}

function TradeTable({ trades, currentPrices, onReplay }: { trades: PaperTrade[]; currentPrices: Record<string, number>; onReplay?: (trade: PaperTrade) => void }) {
  return <div className="scanner"><table><thead><tr>{["Symbol", "Side", "Entry Time", "Entry Score", "Fragility", "Entry Price", "Current / Exit", "PnL", "Return", "MFE", "MAE", "Exit Reason"].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{trades.map(trade => <tr key={trade.id} onClick={() => trade.status === "CLOSED" && onReplay?.(trade)}><td>{trade.symbol}</td><td>{trade.side}</td><td>{new Date(trade.opened_at).toLocaleString()}</td><td>{number(trade.entry_activity_score as number)}</td><td>{number(trade.entry_liquidity_fragility as number)}</td><td>{number(trade.entry_price)}</td><td>{number(trade.status === "OPEN" ? currentPrices[trade.symbol] : trade.exit_price)}</td><td className={(trade.net_pnl ?? 0) >= 0 ? "positive" : "negative"}>{number(trade.net_pnl)}</td><td>{percent(trade.return_pct)}</td><td>{percent(trade.max_favorable_excursion_pct)}</td><td>{percent(trade.max_adverse_excursion_pct)}</td><td>{trade.exit_reason ?? "OPEN"}</td></tr>)}</tbody></table></div>;
}

function SignalMetrics({ snapshot }: { snapshot: Detail }) {
  const signal = (snapshot.trade_signal as Detail | undefined) ?? snapshot;
  return <dl className="signal-metrics">{["activity_score", "liquidity_fragility", "buy_pressure", "sell_pressure", "spot_cvd_5m", "perp_cvd_5m", "oi_change_5m", "funding", "move_type", "cross_exchange_state"].map(key => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof signal[key] === "number" ? number(signal[key] as number, 4) : String(signal[key] ?? "—")}</dd></div>)}</dl>;
}
type BreakdownSummary = { total_trades: number; win_rate: number; net_pnl: number; profit_factor: number | null; average_return_pct: number };

function PerformanceBreakdowns({ breakdowns }: { breakdowns?: Record<string, Record<string, BreakdownSummary>> }) {
  if (!breakdowns) return null;
  const sections = [
    { key: "move_type", title: "PERFORMANCE BY MOVE TYPE" },
    { key: "cross_exchange", title: "PERFORMANCE BY CROSS-EXCHANGE STATE" },
    { key: "activity_score", title: "PERFORMANCE BY ENTRY ACTIVITY SCORE" },
    { key: "liquidity_fragility", title: "PERFORMANCE BY ENTRY LIQUIDITY FRAGILITY" },
  ] as const;
  const activeSections = sections.filter(({ key }) => Object.values(breakdowns[key] ?? {}).some(s => s.total_trades > 0));
  if (activeSections.length === 0) return null;
  return <div className="breakdown-grid">
    {activeSections.map(({ key, title }) => {
      const groups = breakdowns[key] ?? {};
      return <div key={key} className="breakdown-card">
        <h3>{title}</h3>
        <table>
          <thead><tr><th>Category</th><th>Trades</th><th>Win Rate</th><th>Net PnL</th><th>Profit Factor</th></tr></thead>
          <tbody>
            {Object.entries(groups).map(([name, s]) => <tr key={name}><td>{name}</td><td>{s.total_trades}</td><td>{percent(s.win_rate)}</td><td className={s.net_pnl >= 0 ? "positive" : "negative"}>{number(s.net_pnl)}</td><td>{s.profit_factor != null ? number(s.profit_factor) : "—"}</td></tr>)}
          </tbody>
        </table>
      </div>;
    })}
  </div>;
}

