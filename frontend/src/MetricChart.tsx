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
    const ascendingPoints: { time: UTCTimestamp; value: number }[] = [];
    let lastPointSec = -Infinity;
    for (const point of data) {
      const sec = Math.floor(point.timestamp / 1000);
      if (sec > lastPointSec && Number.isFinite(point.price)) {
        ascendingPoints.push({ time: sec as UTCTimestamp, value: point.price });
        lastPointSec = sec;
      }
    }
    series.setData(ascendingPoints);
    const validMarkers = markers.flatMap(marker => {
      const sec = Math.floor(new Date(marker.timestamp).getTime() / 1000);
      return Number.isFinite(sec) && Number.isFinite(marker.price) ? [{
        time: sec as UTCTimestamp,
        position: marker.position,
        color: marker.color,
        shape: "circle" as const,
        text: `${marker.label} ${marker.price.toFixed(2)}`,
      }] : [];
    });
    if (validMarkers.length > 0) {
      createSeriesMarkers(series, validMarkers);
    }
    const resize = new ResizeObserver(entries => chart.applyOptions({ width: entries[0].contentRect.width }));
    resize.observe(host.current);
    return () => { resize.disconnect(); chart.remove(); };
  }, [data, markers]);
  return <div className="chart" ref={host}/>;
}
