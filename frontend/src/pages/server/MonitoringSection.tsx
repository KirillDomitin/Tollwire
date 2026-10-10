import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import { api, unwrap, type Schemas } from "../../api/client";
import { Notice } from "../../components/Notice";
import { TimeChart, type ChartSeries } from "../../components/TimeChart";
import { currentLanguage } from "../../i18n/format";
import { exactTime, timeAgo } from "../../lib/time";
import { formatBitRate, formatBytes, formatByteRate, formatPercent, share } from "../../lib/units";
import styles from "./ServerPage.module.css";

type MetricsRange = Schemas["MetricsRange"];
type MetricPoint = Schemas["MetricPoint"];

const RANGES: MetricsRange[] = ["1h", "24h", "7d", "30d"];
const SHORT_RANGES: MetricsRange[] = ["1h", "24h"];

// Целые — как есть, средние за час (0,5 клиента) — с одним знаком, а не округлением до целого.
const count = (value: number) =>
  Number.isInteger(value)
    ? String(value)
    : new Intl.NumberFormat(currentLanguage(), { maximumFractionDigits: 1 }).format(value);
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
  const { t } = useTranslation();
  const [range, setRange] = useState<MetricsRange>("24h");
  const metrics = useQuery({
    queryKey: ["server", serverId, "metrics", range],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/servers/{server_id}/monitoring", {
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
      {
        label: hourly ? t("servers.monitoring.series.cpuAverage") : t("servers.monitoring.series.cpu"),
        values: pick("cpu_percent"),
        tone: "primary",
      },
    ];
    if (hourly) {
      cpu.push({
        label: t("servers.monitoring.series.cpuPeak"),
        values: pick("cpu_percent_max"),
        tone: "peak",
      });
    }
    const online: ChartSeries[] = [
      {
        label: hourly ? t("servers.monitoring.series.onlineAverage") : t("servers.monitoring.series.online"),
        values: pick("clients_online"),
        tone: "primary",
      },
    ];
    if (hourly) {
      online.push({
        label: t("servers.monitoring.series.onlinePeak"),
        values: pick("clients_online_max"),
        tone: "peak",
      });
    }
    return {
      times,
      cpu,
      online,
      network: [
        {
          label: t("servers.monitoring.series.incoming"),
          values: pick("net_rx_bytes_per_s"),
          tone: "secondary",
        },
        {
          label: t("servers.monitoring.series.outgoing"),
          values: pick("net_tx_bytes_per_s"),
          tone: "primary",
        },
      ] satisfies ChartSeries[],
      memory: [
        {
          label: t("servers.monitoring.series.memory"),
          values: points.map((point) => share(point.mem_used_bytes, point.mem_total_bytes)),
          tone: "primary",
        },
      ] satisfies ChartSeries[],
      disk: [
        {
          label: t("servers.monitoring.series.read"),
          values: pick("disk_read_bytes_per_s"),
          tone: "secondary",
        },
        {
          label: t("servers.monitoring.series.write"),
          values: pick("disk_write_bytes_per_s"),
          tone: "primary",
        },
      ] satisfies ChartSeries[],
      latest: latestPoint(points),
      timeScale: SHORT_RANGES.includes(range) ? ("hours" as const) : ("days" as const),
    };
  }, [metrics.data, range, t]);

  const latest = charts.latest;

  return (
    <section className={styles.section} aria-labelledby="monitoring-title">
      <div className={styles.sectionHead}>
        <h2 id="monitoring-title" className={styles.sectionTitle}>
          {t("servers.monitoring.title")}
        </h2>
        <div className={styles.ranges} role="group" aria-label={t("servers.monitoring.rangeLabel")}>
          {RANGES.map((value) => (
            <button
              key={value}
              type="button"
              className={styles.rangeButton}
              aria-pressed={range === value}
              onClick={() => setRange(value)}
            >
              {t(`servers.monitoring.ranges.${value}`)}
            </button>
          ))}
        </div>
      </div>

      {metrics.isError && <Notice>{metrics.error.message}</Notice>}
      {metrics.data && metrics.data.points.length === 0 && (
        <p className={styles.hint}>{t("servers.monitoring.empty")}</p>
      )}

      {latest && (
        <>
          <dl className={styles.metricsSummary}>
            <div>
              <dt>CPU</dt>
              <dd>{formatPercent(latest.cpu_percent)}</dd>
            </div>
            <div>
              <dt>{t("servers.monitoring.summary.memory")}</dt>
              <dd>
                {t("servers.monitoring.summary.of", {
                  used: formatBytes(latest.mem_used_bytes),
                  total: formatBytes(latest.mem_total_bytes),
                })}
              </dd>
            </div>
            <div>
              <dt>{t("servers.monitoring.summary.disk")}</dt>
              <dd>
                {t("servers.monitoring.summary.of", {
                  used: formatBytes(latest.disk_used_bytes),
                  total: formatBytes(latest.disk_total_bytes),
                })}
              </dd>
            </div>
            <div>
              <dt>{t("servers.monitoring.summary.network")}</dt>
              <dd>
                <span className={styles.nowrap}>{formatBitRate(latest.net_rx_bytes_per_s)}</span> /{" "}
                <span className={styles.nowrap}>{formatBitRate(latest.net_tx_bytes_per_s)}</span>
              </dd>
            </div>
            <div>
              <dt>{t("servers.monitoring.summary.clientsOnline")}</dt>
              <dd>
                {latest.clients_online === null || latest.clients_online === undefined
                  ? "—"
                  : t("servers.monitoring.summary.of", {
                      used: count(latest.clients_online),
                      total: count(latest.clients_total ?? 0),
                    })}
              </dd>
            </div>
          </dl>
          <p className={styles.hint}>
            {metrics.data?.resolution === "hour" ? `${t("servers.monitoring.hourlyNote")} ` : ""}
            <Trans
              i18nKey="servers.monitoring.lastSample"
              values={{ when: timeAgo(latest.ts) }}
              components={{ time: <time dateTime={latest.ts} title={exactTime(latest.ts)} /> }}
            />
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
              title={t("servers.monitoring.charts.network")}
              times={charts.times}
              series={charts.network}
              format={formatBitRate}
              timeScale={charts.timeScale}
            />
            <TimeChart
              title={t("servers.monitoring.charts.memory")}
              times={charts.times}
              series={charts.memory}
              format={percent}
              timeScale={charts.timeScale}
              range={PERCENT_RANGE}
            />
            <TimeChart
              title={t("servers.monitoring.charts.clients")}
              times={charts.times}
              series={charts.online}
              format={count}
              timeScale={charts.timeScale}
              integer
            />
            <TimeChart
              title={t("servers.monitoring.charts.disk")}
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
