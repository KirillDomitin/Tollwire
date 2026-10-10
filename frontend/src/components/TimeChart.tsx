import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

import i18n from "../i18n";
import { currentLanguage, memoByLanguage } from "../i18n/format";
import styles from "./TimeChart.module.css";

export type SeriesTone = "primary" | "secondary" | "peak";

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
  primary: "--pine",
  secondary: "--signal-process",
  peak: "--signal-warn",
};

function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

/** Тема сменилась (системная или явная) — график перерисовывается, чтобы взять новые цвета. */
function useThemeVersion(): number {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const bump = () => setVersion((value) => value + 1);
    const observer = new MutationObserver(bump);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const media =
      typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    media?.addEventListener("change", bump);
    return () => {
      observer.disconnect();
      media?.removeEventListener("change", bump);
    };
  }, []);
  return version;
}

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
  const themeVersion = useThemeVersion();

  useEffect(() => {
    const target = holder.current;
    if (!target) return;
    const axisColor = token("--pine-faint");
    const gridColor = token("--rule");
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
          grid: { stroke: gridColor, width: 1 },
          ticks: { stroke: gridColor, width: 1 },
          values: (_, splits) => splits.map((seconds) => timeLabel.format(new Date(seconds * 1000))),
        },
        {
          stroke: axisColor,
          size: 72,
          incrs: integer ? [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000] : undefined,
          grid: { stroke: gridColor, width: 1 },
          ticks: { stroke: gridColor, width: 1 },
          values: (_, splits) => splits.map((value) => format(value)),
        },
      ],
      series: [
        {
          label: i18n.t("servers.monitoring.series.time"),
          value: (_, seconds) => (seconds ? new Date(seconds * 1000).toLocaleString(currentLanguage()) : "—"),
        },
        ...series.map((item) => ({
          label: item.label,
          stroke: token(TONE_TOKENS[item.tone]),
          width: item.tone === "peak" ? 1 : 1.75,
          dash: item.tone === "peak" ? [4, 4] : undefined,
          spanGaps: false, // нет замера — разрыв линии, а не «ноль»
          points: { show: false },
          value: (_: uPlot, value: number | null) => (value === null ? "—" : format(value)),
        })),
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
  }, [times, series, format, timeScale, range, integer, height, themeVersion]);

  return (
    <figure className={styles.chart}>
      <figcaption className={styles.title}>{title}</figcaption>
      <div ref={holder} className={styles.plot} />
    </figure>
  );
}
