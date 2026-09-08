import { useEffect, useMemo, useState } from "react";
import { MetricChart, type ChartPoint } from "./MetricChart";

type ScannerRow = { symbol: string; market_cap_rank: number; price: number | null; activity_score: number | null; liquidity_fragility: number | null };
type Detail = Record<string, unknown>;
type SortKey = keyof ScannerRow;
type PaperTrade = Record<string, unknown> & { id: number; symbol: string; side: string; status: string; opened_at: string; entry_price: number; exit_price: number | null; net_pnl: number | null; return_pct: number | null; max_favorable_excursion_pct: number; max_adverse_excursion_pct: number; exit_reason: string | null };
type PaperStats = Record<string, unknown> & { total_trades: number; open_trades: number; win_rate: number; net_pnl: number; profit_factor: number | null };

const number = (value: number | null | undefined, digits = 2) => value == null ? "—" : value.toLocaleString(undefined, { maximumFractionDigits: digits });
const percent = (value: number | null | undefined) => value == null ? "—" : `${value >= 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;
const socketUrl = (path: string) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;

export function App() {
  const [mode, setMode] = useState<"SCANNER" | "PAPER">("SCANNER");
  const [rows, setRows] = useState<ScannerRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
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
    if (!selected || mode !== "SCANNER") return;
    void fetch(`/api/symbol/${selected}`).then(response => response.ok ? response.json() : null).then(setDetail);
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

  return <main className="terminal">
    <header className="toolbar"><strong>QTRADE / CEX LIQUIDITY & FLOW</strong><nav><button className={mode === "SCANNER" ? "active" : ""} onClick={() => setMode("SCANNER")}>SCANNER</button><button className={mode === "PAPER" ? "active" : ""} onClick={() => setMode("PAPER")}>PAPER</button></nav><span>BINANCE <i className="online"/> OKX <i className="online"/></span><span className="connection">{connection}</span></header>
    {mode === "SCANNER" ? <><section className="scanner"><table><thead><tr>{(["symbol", "market_cap_rank", "price", "liquidity_fragility", "activity_score"] as SortKey[]).map(key => <th key={key} onClick={() => chooseSort(key)}>{key.replaceAll("_", " ")}{sort.key === key ? sort.descending ? " ↓" : " ↑" : ""}</th>)}</tr></thead><tbody>{ordered.map(row => <tr key={row.symbol} className={row.symbol === selected ? "selected" : ""} onClick={() => setSelected(row.symbol)}><td>{row.symbol}</td><td>{row.market_cap_rank}</td><td>{number(row.price)}</td><td>{number(row.liquidity_fragility)}</td><td>{number(row.activity_score)}</td></tr>)}</tbody></table></section><section className="detail"><div><h2>{selected ?? "SELECT SYMBOL"}</h2>{detail ? <dl>{Object.entries(detail).filter(([, value]) => typeof value !== "object").map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof value === "number" ? number(value, 4) : String(value)}</dd></div>)}</dl> : <p>Choose a scanner row for exchange-normalized liquidity, flow, and derivatives metrics.</p>}</div><MetricChart data={Array.isArray(detail?.history) ? detail.history as ChartPoint[] : []}/></section></> : <PaperPage stats={stats} positions={positions} trades={trades} replay={replay} onReplay={loadReplay} onCloseReplay={() => setReplay(null)}/>}
  </main>;
}

function PaperPage({ stats, positions, trades, replay, onReplay, onCloseReplay }: { stats: PaperStats | null; positions: PaperTrade[]; trades: PaperTrade[]; replay: { trade: PaperTrade; entry_snapshot: Detail; exit_snapshot: Detail | null; history: { market: ChartPoint[] } } | null; onReplay: (trade: PaperTrade) => void; onCloseReplay: () => void }) {
  if (replay) return <section className="replay"><button onClick={onCloseReplay}>← PAPER TRADES</button><h2>TRADE REPLAY / {replay.trade.symbol} {replay.trade.side}</h2><p>Entry {new Date(replay.trade.opened_at).toLocaleString()} · Activity {number(replay.trade.entry_activity_score as number)} · Fragility {number(replay.trade.entry_liquidity_fragility as number)}</p><p>Exit {replay.trade.exit_reason ?? "OPEN"} · Return {percent(replay.trade.return_pct)}</p><MetricChart data={replay.history.market ?? []}/><SignalMetrics snapshot={replay.entry_snapshot}/></section>;
  return <section className="paper"><div className="stats">{[["TOTAL TRADES", stats?.total_trades], ["WIN RATE", stats ? percent(stats.win_rate) : null], ["NET PNL", stats?.net_pnl == null ? null : number(stats.net_pnl)], ["PROFIT FACTOR", stats?.profit_factor], ["OPEN POSITIONS", stats?.open_trades]].map(([label, value]) => <div key={String(label)}><small>{label}</small><strong>{typeof value === "number" ? number(value) : value ?? "—"}</strong></div>)}</div><h2>OPEN POSITIONS</h2><TradeTable trades={positions}/><h2>TRADE HISTORY</h2><TradeTable trades={trades} onReplay={onReplay}/>{stats?.breakdowns ? <pre className="breakdowns">{JSON.stringify(stats.breakdowns, null, 2)}</pre> : null}</section>;
}

function TradeTable({ trades, onReplay }: { trades: PaperTrade[]; onReplay?: (trade: PaperTrade) => void }) {
  return <div className="scanner"><table><thead><tr>{["Symbol", "Side", "Entry Time", "Entry Score", "Fragility", "Entry Price", "Current / Exit", "PnL", "Return", "MFE", "MAE", "Exit Reason"].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{trades.map(trade => <tr key={trade.id} onClick={() => trade.status === "CLOSED" && onReplay?.(trade)}><td>{trade.symbol}</td><td>{trade.side}</td><td>{new Date(trade.opened_at).toLocaleString()}</td><td>{number(trade.entry_activity_score as number)}</td><td>{number(trade.entry_liquidity_fragility as number)}</td><td>{number(trade.entry_price)}</td><td>{number(trade.exit_price)}</td><td className={(trade.net_pnl ?? 0) >= 0 ? "positive" : "negative"}>{number(trade.net_pnl)}</td><td>{percent(trade.return_pct)}</td><td>{percent(trade.max_favorable_excursion_pct)}</td><td>{percent(trade.max_adverse_excursion_pct)}</td><td>{trade.exit_reason ?? "OPEN"}</td></tr>)}</tbody></table></div>;
}

function SignalMetrics({ snapshot }: { snapshot: Detail }) {
  const signal = (snapshot.trade_signal as Detail | undefined) ?? snapshot;
  return <dl className="signal-metrics">{["activity_score", "liquidity_fragility", "buy_pressure", "sell_pressure", "spot_cvd_5m", "perp_cvd_5m", "oi_change_5m", "funding", "move_type", "cross_exchange_state"].map(key => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof signal[key] === "number" ? number(signal[key] as number, 4) : String(signal[key] ?? "—")}</dd></div>)}</dl>;
}
