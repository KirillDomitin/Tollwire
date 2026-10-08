import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api, unwrap, type Schemas } from "../../api/client";
import { Notice } from "../../components/Notice";
import { TimeChart, type ChartSeries } from "../../components/TimeChart";
import { exactTime, timeAgo } from "../../lib/time";
import { formatBitRate, formatBytes, formatByteRate, formatPercent, share } from "../../lib/units";
import styles from "./ServerPage.module.css";

type MetricsRange = Schemas["MetricsRange"];
type MetricPoint = Schemas["MetricPoint"];

const RANGES: { value: MetricsRange; label: string }[] = [
  { value: "1h", label: "Час" },
  { value: "24h", label: "Сутки" },
  { value: "7d", label: "Неделя" },
  { value: "30d", label: "Месяц" },
];
const SHORT_RANGES: MetricsRange[] = ["1h", "24h"];

// Целые — как есть, средние за час (0,5 клиента) — с одним знаком, а не округлением до целого.
const count = (value: number) =>
  Number.isInteger(value)
    ? String(value)
    : new Intl.NumberFormat("ru", { maximumFractionDigits: 1 }).format(value);
// Константа, а не литерал в JSX: новый массив на каждой отрисовке пересоздавал бы график.
const PERCENT_RANGE: [number, number] = [0, 100];
const percent = (value: number) => formatPercent(value);

/** Последний замер, в котором уже есть скорость (первый замер после перерыва — без неё). */
function latestPoint(points: MetricPoint[]): MetricPoint | null {
  return [...points].reverse().find((point) => point.cpu_percent !== null) ?? points.at(-1) ?? null;
}

/**
 * Замеров не было дольше трёх интервалов (сервер недоступен, воркер стоял) — вставляем пустую точку, чтобы линия
 * прервалась. Иначе график соединил бы края прямой и нарисовал «плавное» изменение, которого никто не видел.
 */
export function breakGaps(points: MetricPoint[], maxGapMs: number): MetricPoint[] {
  const result: MetricPoint[] = [];
  for (const point of points) {
    const previous = result.at(-1);
    if (previous && new Date(point.ts).getTime() - new Date(previous.ts).getTime() > maxGapMs) {
      const gap = Object.fromEntries(Object.keys(point).map((key) => [key, null])) as unknown as MetricPoint;
      result.push({ ...gap, ts: new Date(new Date(previous.ts).getTime() + 1000).toISOString() });
    }
    result.push(point);
  }
  return result;
}

/** Мониторинг сервера: сводка последнего замера и графики за выбранный период. */
export function MonitoringSection({ serverId }: { serverId: string }) {
  const [range, setRange] = useState<MetricsRange>("24h");
  const metrics = useQuery({
    queryKey: ["server", serverId, "metrics", range],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/servers/{server_id}/metrics", {
          params: { path: { server_id: serverId }, query: { range } },
        }),
      ),
    // Короткие периоды — поминутные замеры, их стоит подтягивать чаще; почасовые меняются медленно.
    refetchInterval: SHORT_RANGES.includes(range) ? 60_000 : 300_000,
  });

  const charts = useMemo(() => {
    const hourly = metrics.data?.resolution === "hour";
    const points = breakGaps(metrics.data?.points ?? [], hourly ? 3 * 3_600_000 : 3 * 60_000);
    const times = points.map((point) => new Date(point.ts).getTime() / 1000);
    const pick = (key: keyof MetricPoint) =>
      points.map((point) => (point[key] as number | null | undefined) ?? null);
    const cpu: ChartSeries[] = [
      { label: hourly ? "CPU, среднее" : "CPU", values: pick("cpu_percent"), tone: "primary" },
    ];
    if (hourly) cpu.push({ label: "CPU, пик", values: pick("cpu_percent_max"), tone: "peak" });
    const online: ChartSeries[] = [
      { label: hourly ? "Онлайн, среднее" : "Онлайн", values: pick("clients_online"), tone: "primary" },
    ];
    if (hourly) online.push({ label: "Онлайн, пик", values: pick("clients_online_max"), tone: "peak" });
    return {
      times,
      cpu,
      online,
      network: [
        { label: "Входящий", values: pick("net_rx_bytes_per_s"), tone: "secondary" },
        { label: "Исходящий", values: pick("net_tx_bytes_per_s"), tone: "primary" },
      ] satisfies ChartSeries[],
      memory: [
        {
          label: "Память",
          values: points.map((point) => share(point.mem_used_bytes, point.mem_total_bytes)),
          tone: "primary",
        },
      ] satisfies ChartSeries[],
      disk: [
        { label: "Чтение", values: pick("disk_read_bytes_per_s"), tone: "secondary" },
        { label: "Запись", values: pick("disk_write_bytes_per_s"), tone: "primary" },
      ] satisfies ChartSeries[],
      latest: latestPoint(points),
      timeScale: SHORT_RANGES.includes(range) ? ("hours" as const) : ("days" as const),
    };
  }, [metrics.data, range]);

  const latest = charts.latest;

  return (
    <section className={styles.section} aria-labelledby="monitoring-title">
      <div className={styles.sectionHead}>
        <h2 id="monitoring-title" className={styles.sectionTitle}>
          Мониторинг
        </h2>
        <div className={styles.ranges} role="group" aria-label="Период графиков">
          {RANGES.map((item) => (
            <button
              key={item.value}
              type="button"
              className={styles.rangeButton}
              aria-pressed={range === item.value}
              onClick={() => setRange(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {metrics.isError && <Notice>{metrics.error.message}</Notice>}
      {metrics.data && metrics.data.points.length === 0 && (
        <p className={styles.hint}>
          Замеров за этот период нет. Панель снимает их раз в минуту по SSH — первые появятся вскоре после
          запуска воркера; недоступный сервер даёт пропуски.
        </p>
      )}

      {latest && (
        <>
          <dl className={styles.metricsSummary}>
            <div>
              <dt>CPU</dt>
              <dd>{formatPercent(latest.cpu_percent)}</dd>
            </div>
            <div>
              <dt>Память</dt>
              <dd>
                {formatBytes(latest.mem_used_bytes)} из {formatBytes(latest.mem_total_bytes)}
              </dd>
            </div>
            <div>
              <dt>Диск</dt>
              <dd>
                {formatBytes(latest.disk_used_bytes)} из {formatBytes(latest.disk_total_bytes)}
              </dd>
            </div>
            <div>
              <dt>Сеть ↓ / ↑</dt>
              <dd>
                <span className={styles.nowrap}>{formatBitRate(latest.net_rx_bytes_per_s)}</span> /{" "}
                <span className={styles.nowrap}>{formatBitRate(latest.net_tx_bytes_per_s)}</span>
              </dd>
            </div>
            <div>
              <dt>Клиенты онлайн</dt>
              <dd>
                {latest.clients_online === null || latest.clients_online === undefined
                  ? "—"
                  : `${count(latest.clients_online)} из ${count(latest.clients_total ?? 0)}`}
              </dd>
            </div>
          </dl>
          <p className={styles.hint}>
            {metrics.data?.resolution === "hour" ? "Средние за час, сводка — по последнему часу. " : ""}
            Последний замер{" "}
            <time dateTime={latest.ts} title={exactTime(latest.ts)}>
              {timeAgo(latest.ts)}
            </time>
            .
          </p>
          <div className={styles.charts}>
            <TimeChart
              title="CPU"
              times={charts.times}
              series={charts.cpu}
              format={percent}
              timeScale={charts.timeScale}
              range={PERCENT_RANGE}
            />
            <TimeChart
              title="Сеть"
              times={charts.times}
              series={charts.network}
              format={formatBitRate}
              timeScale={charts.timeScale}
            />
            <TimeChart
              title="Память"
              times={charts.times}
              series={charts.memory}
              format={percent}
              timeScale={charts.timeScale}
              range={PERCENT_RANGE}
            />
            <TimeChart
              title="Клиенты онлайн"
              times={charts.times}
              series={charts.online}
              format={count}
              timeScale={charts.timeScale}
              integer
            />
            <TimeChart
              title="Диск: чтение и запись"
              times={charts.times}
              series={charts.disk}
              format={formatByteRate}
              timeScale={charts.timeScale}
            />
          </div>
        </>
      )}
    </section>
  );
}
