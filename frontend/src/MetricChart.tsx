import { ColorType, createChart, createSeriesMarkers, LineSeries, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";

export type ChartPoint = { timestamp: number; price: number };
type TradeMarker = { timestamp: string; price: number; label: string; color: string; position: "aboveBar" | "belowBar" };

export function MetricChart({ data, markers = [] }: { data: ChartPoint[]; markers?: TradeMarker[] }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current) return;
    const chart = createChart(host.current, { height: 220, layout: { background: { type: ColorType.Solid, color: "#10141d" }, textColor: "#9aa7ba" }, grid: { vertLines: { color: "#202938" }, horzLines: { color: "#202938" } } });
    const series = chart.addSeries(LineSeries, { color: "#42d392", lineWidth: 2 });
    series.setData(data.map(point => ({ time: Math.floor(point.timestamp / 1000) as UTCTimestamp, value: point.price })));
    createSeriesMarkers(series, markers.map(marker => ({ time: Math.floor(new Date(marker.timestamp).getTime() / 1000) as UTCTimestamp, position: marker.position, color: marker.color, shape: "circle", text: `${marker.label} ${marker.price.toFixed(2)}` })));
    const resize = new ResizeObserver(entries => chart.applyOptions({ width: entries[0].contentRect.width }));
    resize.observe(host.current);
    return () => { resize.disconnect(); chart.remove(); };
  }, [data, markers]);
  return <div className="chart" ref={host}/>;
}
