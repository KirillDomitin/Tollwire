import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

import i18n from "../i18n";
import { currentLanguage, memoByLanguage } from "../i18n/format";
import styles from "./TimeChart.module.css";

/** Цвет линии: синий, зелёный, фиолетовый — как в макете; peak — пунктир янтарного цвета для пиковых значений. */
export type SeriesTone = "blue" | "green" | "violet" | "peak";

export interface ChartSeries {
  label: string;
  values: (number | null)[];
  tone: SeriesTone;
}

interface TimeChartProps {
  title: string;
  /** Время точек, unix-секунды. */
  times: number[];
  series: ChartSeries[];
  /** Подпись значения на оси и в легенде. */
  format: (value: number) => string;
  /** Подписи времени: короткие периоды — часы, длинные — дни. */
  timeScale: "hours" | "days";
  /** Нижняя/верхняя граница шкалы (например, 0–100 для процентов). */
  range?: [number, number];
  /** Шкала только целыми делениями — для счётчиков (клиенты): «0, 1, 2», а не «0, 0,5, 1». */
  integer?: boolean;
  height?: number;
}

const TONE_TOKENS: Record<SeriesTone, string> = {
  blue: "--chart-blue",
  green: "--chart-green",
  violet: "--chart-violet",
  peak: "--signal-warn",
};

function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

/** Цвет #rrggbb с прозрачностью — для заливки под линией; не hex — null (заливки не будет). */
function withAlpha(color: string, alpha: number): string | null {
  const match = /^#([0-9a-f]{6})$/i.exec(color);
  if (!match?.[1]) return null;
  const value = parseInt(match[1], 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
}

const FONT = '12px "Golos Text Variable", "Golos Text", system-ui, sans-serif';

const hourFormat = memoByLanguage(
  (lng) => new Intl.DateTimeFormat(lng, { hour: "2-digit", minute: "2-digit" }),
);
const dayFormat = memoByLanguage((lng) => new Intl.DateTimeFormat(lng, { day: "numeric", month: "short" }));

/** График временного ряда на uPlot: лёгкий (~50 КБ) и быстрый на тысячах точек. */
export function TimeChart({
  title,
  times,
  series,
  format,
  timeScale,
  range,
  integer = false,
  height = 170,
}: TimeChartProps) {
  const holder = useRef<HTMLDivElement>(null);
  const chart = useRef<uPlot | null>(null);

  useEffect(() => {
    const target = holder.current;
    if (!target) return;
    const axisColor = token("--text-muted");
    const gridColor = token("--chart-grid");
    const timeLabel = timeScale === "hours" ? hourFormat() : dayFormat();
    const options: uPlot.Options = {
      width: target.clientWidth || 600,
      height,
      scales: { x: { time: true }, y: range ? { range } : {} },
      legend: { live: true },
      cursor: { drag: { x: false, y: false } },
      axes: [
        {
          stroke: axisColor,
          font: FONT,
          grid: { stroke: gridColor, width: 1 },
          ticks: { show: false },
          values: (_, splits) => splits.map((seconds) => timeLabel.format(new Date(seconds * 1000))),
        },
        {
          stroke: axisColor,
          font: FONT,
          size: 72,
          incrs: integer ? [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000] : undefined,
          grid: { stroke: gridColor, width: 1 },
          ticks: { show: false },
          values: (_, splits) => splits.map((value) => format(value)),
        },
      ],
      series: [
        {
          label: i18n.t("servers.monitoring.series.time"),
          value: (_, seconds) => (seconds ? new Date(seconds * 1000).toLocaleString(currentLanguage()) : "—"),
        },
        ...series.map((item) => {
          const color = token(TONE_TOKENS[item.tone]);
          const top = withAlpha(color, 0.26);
          const bottom = withAlpha(color, 0);
          return {
            label: item.label,
            stroke: color,
            width: item.tone === "peak" ? 1 : 2,
            dash: item.tone === "peak" ? [4, 4] : undefined,
            // Мягкая заливка под линией, как в макете; у пикового пунктира заливки нет.
            fill:
              item.tone !== "peak" && top && bottom
                ? (u: uPlot) => {
                    // Легенда запрашивает заливку до первой отрисовки, когда области графика ещё нет, — тогда сплошной цвет.
                    const { top: y, height } = u.bbox;
                    if (!Number.isFinite(y) || !Number.isFinite(height) || height <= 0) return top;
                    const gradient = u.ctx.createLinearGradient(0, y, 0, y + height);
                    gradient.addColorStop(0, top);
                    gradient.addColorStop(1, bottom);
                    return gradient;
                  }
                : undefined,
            spanGaps: false, // нет замера — разрыв линии, а не «ноль»
            points: { show: false },
            value: (_: uPlot, value: number | null) => (value === null ? "—" : format(value)),
          };
        }),
      ],
    };
    const data = [times, ...series.map((item) => item.values)] as uPlot.AlignedData;
    chart.current = new uPlot(options, data, target);

    const resize =
      typeof ResizeObserver === "function"
        ? new ResizeObserver(() => chart.current?.setSize({ width: target.clientWidth, height }))
        : null;
    resize?.observe(target);
    return () => {
      resize?.disconnect();
      chart.current?.destroy();
      chart.current = null;
    };
    // Данные и вид перестраиваем целиком: точек немного, а так проще не рассинхронизировать опции и данные.
  }, [times, series, format, timeScale, range, integer, height]);

  return (
    <figure className={styles.chart}>
      <figcaption className={styles.title}>{title}</figcaption>
      <div ref={holder} className={styles.plot} />
    </figure>
  );
}
