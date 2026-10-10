import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { api, unwrap, type Schemas, type Server } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Button } from "../components/Button";
import { Notice } from "../components/Notice";
import { fleetSummary, serverSignal } from "../lib/serverSignal";
import { exactTime, timeAgo } from "../lib/time";
import { formatPercent } from "../lib/units";
import { AddServerDialog } from "./server/AddServerDialog";
import styles from "./Ledger.module.css";

type LatestMetrics = Schemas["LatestMetrics"];

/** Состояние парка меняют фоновые задачи (health, sync) — список подтягивается сам раз в 30 секунд. */
const REFRESH_MS = 30_000;

export function ServersPage() {
  const { can } = useAuth();
  const servers = useQuery({
    queryKey: ["servers"],
    queryFn: () => unwrap(api.GET("/api/v1/servers")),
    refetchInterval: REFRESH_MS,
  });
  // Последние замеры мониторинга — для столбцов «Клиенты» и «CPU». Замеры раз в минуту — и подтягиваем так же.
  const metrics = useQuery({
    queryKey: ["metrics", "latest"],
    queryFn: () => unwrap(api.GET("/api/v1/monitoring/latest")),
    refetchInterval: 60_000,
  });
  const latest = freshMetrics(metrics.data ?? [], metrics.dataUpdatedAt);

  return (
    <section aria-labelledby="servers-title">
      <header className={styles.header}>
        <h1 id="servers-title" className={styles.title}>
          Серверы
        </h1>
        {servers.data && servers.data.length > 0 && (
          <p className={styles.summary}>{fleetSummary(servers.data)}</p>
        )}
        {can("servers:manage") && (
          <div className={styles.headerAction}>
            <AddServerDialog />
          </div>
        )}
      </header>

      {servers.isPending && <p className={styles.quiet}>Загружаем серверы…</p>}

      {servers.isError && (
        <Notice
          action={
            <Button variant="quiet" onClick={() => void servers.refetch()}>
              Повторить
            </Button>
          }
        >
          {servers.error.message}
        </Notice>
      )}

      {servers.data?.length === 0 && (
        <p className={styles.quiet}>
          Серверов пока нет. Добавленный сервер появится здесь вместе со статусом, а подготовить его можно по
          шагам на его странице.
        </p>
      )}

      {servers.data && servers.data.length > 0 && <ServerTable servers={servers.data} metrics={latest} />}
    </section>
  );
}

/** Замер старше 5 минут на момент получения не показываем: устаревшая цифра хуже прочерка. */
const STALE_MS = 5 * 60_000;

export function freshMetrics(items: LatestMetrics[], receivedAt: number): Map<string, LatestMetrics> {
  return new Map(
    items
      .filter((item) => receivedAt - new Date(item.ts).getTime() <= STALE_MS)
      .map((item) => [item.server_id, item]),
  );
}

function ServerTable({ servers, metrics }: { servers: Server[]; metrics: Map<string, LatestMetrics> }) {
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th scope="col">Сервер</th>
          <th scope="col">Состояние</th>
          <th scope="col">Клиенты</th>
          <th scope="col">CPU</th>
          <th scope="col">Проверка</th>
          <th scope="col">Ключ хоста</th>
        </tr>
      </thead>
      <tbody>
        {servers.map((server) => {
          const { signal, label } = serverSignal(server);
          const metric = metrics.get(server.id);
          return (
            <tr key={server.id} className={styles.row} data-signal={signal}>
              <th scope="row" className={styles.name}>
                <Link to={`/servers/${server.id}`} className={styles.link}>
                  {server.name}
                </Link>
                <span className={styles.address}>
                  {server.host}
                  {server.ssh_port !== 22 && `:${server.ssh_port}`}
                </span>
              </th>
              <td>
                <span className={styles.state} data-signal={signal}>
                  {label}
                </span>
              </td>
              <td className={styles.meta} title="Онлайн из всех клиентов на сервере">
                {metric?.clients_online === null || metric?.clients_online === undefined
                  ? "—"
                  : `${metric.clients_online} из ${metric.clients_total ?? 0}`}
              </td>
              <td className={styles.meta}>{formatPercent(metric?.cpu_percent)}</td>
              <td className={styles.meta}>
                {server.last_health_at ? (
                  <time dateTime={server.last_health_at} title={exactTime(server.last_health_at)}>
                    {timeAgo(server.last_health_at)}
                  </time>
                ) : (
                  "ещё не проверялся"
                )}
              </td>
              <td className={styles.meta}>
                {server.ssh_host_key_fingerprint ? (
                  <code className={styles.fingerprint} title={server.ssh_host_key_fingerprint}>
                    {server.ssh_host_key_fingerprint.slice(0, 18)}…
                  </code>
                ) : (
                  <span className={styles.unpinned}>не закреплён</span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
