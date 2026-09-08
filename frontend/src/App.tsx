import { useEffect, useMemo, useState } from "react";
import { MetricChart, type ChartPoint } from "./MetricChart";

type ScannerRow = { symbol: string; market_cap_rank: number; price: number | null; activity_score: number | null; liquidity_fragility: number | null };
type Detail = Record<string, unknown>;
type SortKey = keyof ScannerRow;

const number = (value: number | null | undefined, digits = 2) => value == null ? "—" : value.toLocaleString(undefined, { maximumFractionDigits: digits });
const socketUrl = (path: string) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;

export function App() {
  const [rows, setRows] = useState<ScannerRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [sort, setSort] = useState<{ key: SortKey; descending: boolean }>({ key: "activity_score", descending: true });
  const [connection, setConnection] = useState("CONNECTING");

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
      } else {
        const update = message.data;
        setRows(current => [...current.filter(row => row.symbol !== update.symbol), update]);
      }
    };
    return () => socket.close();
  }, []);

  useEffect(() => {
    if (!selected) return;
    void fetch(`/api/symbol/${selected}`).then(response => response.ok ? response.json() : null).then(setDetail);
    const socket = new WebSocket(socketUrl(`/ws/symbol/${selected}`));
    socket.onmessage = ({ data }) => {
      const message = JSON.parse(data) as { type: string; data: Detail };
      if (message.type === "symbol") setDetail(message.data);
    };
    return () => socket.close();
  }, [selected]);

  const ordered = useMemo(() => [...rows].sort((left, right) => {
    const a = left[sort.key] ?? (sort.descending ? -Infinity : Infinity);
    const b = right[sort.key] ?? (sort.descending ? -Infinity : Infinity);
    return (a < b ? -1 : a > b ? 1 : 0) * (sort.descending ? -1 : 1);
  }), [rows, sort]);
  const chooseSort = (key: SortKey) => setSort(current => ({ key, descending: current.key === key ? !current.descending : key !== "symbol" }));
  const history = Array.isArray(detail?.history) ? detail.history as ChartPoint[] : [];

  return <main className="terminal">
    <header className="toolbar"><strong>QTRADE / CEX LIQUIDITY & FLOW</strong><span>BINANCE <i className="online"/> OKX <i className="online"/></span><span className="connection">{connection}</span></header>
    <section className="scanner"><table><thead><tr>{(["symbol", "market_cap_rank", "price", "liquidity_fragility", "activity_score"] as SortKey[]).map(key => <th key={key} onClick={() => chooseSort(key)}>{key.replaceAll("_", " ")}{sort.key === key ? sort.descending ? " ↓" : " ↑" : ""}</th>)}</tr></thead><tbody>{ordered.map(row => <tr key={row.symbol} className={row.symbol === selected ? "selected" : ""} onClick={() => setSelected(row.symbol)}><td>{row.symbol}</td><td>{row.market_cap_rank}</td><td>{number(row.price)}</td><td>{number(row.liquidity_fragility)}</td><td>{number(row.activity_score)}</td></tr>)}</tbody></table></section>
    <section className="detail"><div><h2>{selected ?? "SELECT SYMBOL"}</h2>{detail ? <dl>{Object.entries(detail).filter(([, value]) => typeof value !== "object").map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof value === "number" ? number(value, 4) : String(value)}</dd></div>)}</dl> : <p>Choose a scanner row for exchange-normalized liquidity, flow, and derivatives metrics.</p>}</div><MetricChart data={history}/></section>
  </main>;
}
