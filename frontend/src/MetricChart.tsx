import {
  ColorType,
  createChart,
  createSeriesMarkers,
  LineSeries,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

export type ChartPoint = { timestamp: number; price: number };
type TradeMarker = { timestamp: string; price: number; label: string; color: string; position: "aboveBar" | "belowBar" };

export function MetricChart({ data, markers = [] }: { data: ChartPoint[]; markers?: TradeMarker[] }) {
  const host = useRef<HTMLDivElement>(null);
  const seriesRef = useRef<ISeriesApi<"Line", Time> | null>(null);
  const markersPluginRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const chart = createChart(host.current, {
      height: 250,
      layout: { background: { type: ColorType.Solid, color: "#10141d" }, textColor: "#9aa7ba" },
      grid: { vertLines: { color: "#1c2433" }, horzLines: { color: "#1c2433" } },
      timeScale: { timeVisible: true, secondsVisible: true, borderColor: "#273142" },
      rightPriceScale: { borderColor: "#273142" },
    });
    const series = chart.addSeries(LineSeries, {
      color: "#42d392",
      lineWidth: 2,
      priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
    });
    const markersPlugin = createSeriesMarkers(series, []);
    seriesRef.current = series;
    markersPluginRef.current = markersPlugin;

    const resize = new ResizeObserver(entries => chart.applyOptions({ width: entries[0].contentRect.width }));
    resize.observe(host.current);

    return () => {
      resize.disconnect();
      chart.remove();
      seriesRef.current = null;
      markersPluginRef.current = null;
    };
  }, []);

  useEffect(() => {
    const series = seriesRef.current;
    const markersPlugin = markersPluginRef.current;
    if (!series || !markersPlugin) return;

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
    markersPlugin.setMarkers(validMarkers);
  }, [data, markers]);

  return <div className="chart" ref={host}/>;
}
