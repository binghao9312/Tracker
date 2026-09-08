import { ColorType, createChart, LineSeries, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";

export type ChartPoint = { timestamp: number; price: number };

export function MetricChart({ data }: { data: ChartPoint[] }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current) return;
    const chart = createChart(host.current, { height: 220, layout: { background: { type: ColorType.Solid, color: "#10141d" }, textColor: "#9aa7ba" }, grid: { vertLines: { color: "#202938" }, horzLines: { color: "#202938" } } });
    const series = chart.addSeries(LineSeries, { color: "#42d392", lineWidth: 2 });
    series.setData(data.map(point => ({ time: Math.floor(point.timestamp / 1000) as UTCTimestamp, value: point.price })));
    const resize = new ResizeObserver(entries => chart.applyOptions({ width: entries[0].contentRect.width }));
    resize.observe(host.current);
    return () => { resize.disconnect(); chart.remove(); };
  }, [data]);
  return <div className="chart" ref={host}/>;
}
