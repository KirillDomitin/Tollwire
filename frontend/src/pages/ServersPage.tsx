import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { api, unwrap, type Schemas, type Server } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Button } from "../components/Button";
import table from "../components/DataTable.module.css";
import { ActivityIcon, SearchIcon, ServerIcon, UsersIcon } from "../components/icons";
import { SortHeader } from "../components/SortHeader";
import { nextSort, serverRank, sortRows, type Sort } from "../lib/tableSort";
import { Notice } from "../components/Notice";
import { PageHeader } from "../components/PageHeader";
import { StatCard } from "../components/StatCard";
import { SIGNAL_TONE, StatusPill } from "../components/StatusPill";
import { TextField } from "../components/TextField";
import { splitFlag } from "../lib/flags";
import { fleetSummary, serverSignal } from "../lib/serverSignal";
import { exactTime, timeAgo } from "../lib/time";
import { formatPercent } from "../lib/units";
import { freshMetrics, useLatestMetrics } from "./latestMetrics";
import { AddServerDialog } from "./server/AddServerDialog";
import styles from "./ServersPage.module.css";

type LatestMetrics = Schemas["LatestMetrics"];

export { freshMetrics };

/** Состояние парка меняют фоновые задачи (health, sync) — список подтягивается сам раз в 30 секунд. */
const REFRESH_MS = 30_000;

type ServerSort = "name" | "status" | "clients";
type ServerFilter = "all" | "online" | "offline";

export function ServersPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<ServerFilter>("all");
  const [sort, setSort] = useState<Sort<ServerSort>>(null);
  const servers = useQuery({
    queryKey: ["servers"],
    queryFn: () => unwrap(api.GET("/api/v1/servers")),
    refetchInterval: REFRESH_MS,
  });
  // Последние замеры мониторинга — для столбцов «Клиенты» и «Нагрузка» и плитки «Всего клиентов».
  const latest = useLatestMetrics();
  const all = servers.data ?? [];
  const query = search.trim().toLowerCase();
  const groups = { online: [] as Server[], offline: [] as Server[] };
  // Новые и устанавливаемые серверы видны только в общем списке.
  for (const server of all) {
    const { signal } = serverSignal(server);
    if (signal === "ok" || signal === "warn") groups.online.push(server);
    if (signal === "down") groups.offline.push(server);
  }
  const filtered = filter === "all" ? all : groups[filter];
  const shown = query
    ? filtered.filter((server) => `${server.name} ${server.host}`.toLowerCase().includes(query))
    : filtered;

  return (
    <section aria-labelledby="servers-title">
      <PageHeader
        id="servers-title"
        title={t("servers.list.title")}
        subtitle={t("servers.list.subtitle")}
        actions={can("servers:manage") ? <AddServerDialog /> : undefined}
      />

      {servers.isPending && <p className={styles.quiet}>{t("servers.list.loading")}</p>}

      {servers.isError && (
        <Notice
          action={
            <Button variant="secondary" onClick={() => void servers.refetch()}>
              {t("servers.list.retry")}
            </Button>
          }
        >
          {servers.error.message}
        </Notice>
      )}

      {servers.data?.length === 0 && <p className={styles.quiet}>{t("servers.list.empty")}</p>}

      {all.length > 0 && (
        <>
          <Stats
            servers={all}
            metrics={latest}
            online={groups.online.length}
            offline={groups.offline.length}
            filter={filter}
            onFilter={setFilter}
          />
          <div className={styles.toolbar}>
            <TextField
              className={styles.search}
              type="search"
              label={t("servers.list.search")}
              hideLabel
              placeholder={t("servers.list.search")}
              icon={<SearchIcon />}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              autoComplete="off"
            />
          </div>
          {shown.length === 0 ? (
            <p className={styles.quiet}>
              {t(query ? "servers.list.noMatches" : "servers.list.noStatusMatches")}
            </p>
          ) : (
            <ServerTable
              servers={shown}
              metrics={latest}
              sort={sort}
              onSort={(field) => setSort(nextSort(sort, field))}
            />
          )}
          <div className={`${table.foot} ${styles.foot}`}>
            <span>{t("servers.list.shown", { shown: shown.length, total: all.length })}</span>
            <span>{fleetSummary(all)}</span>
          </div>
        </>
      )}
    </section>
  );
}

/** Плитки сводки: сколько серверов, сколько в сети и без связи, сколько клиентов (по свежим замерам). */
function Stats({
  servers,
  metrics,
  online,
  offline,
  filter,
  onFilter,
}: {
  servers: Server[];
  metrics: Map<string, LatestMetrics>;
  online: number;
  offline: number;
  filter: ServerFilter;
  onFilter: (filter: ServerFilter) => void;
}) {
  const { t } = useTranslation();
  const counts = servers.flatMap((server) => {
    const total = metrics.get(server.id)?.clients_total;
    return total === null || total === undefined ? [] : [total];
  });
  const clients = counts.length > 0 ? counts.reduce((sum, value) => sum + value, 0) : null;
  return (
    <div className={styles.stats}>
      <StatCard
        icon={<ServerIcon size={24} />}
        label={t("servers.list.stats.total")}
        value={servers.length}
        action={{ pressed: filter === "all", onClick: () => onFilter("all") }}
      />
      <StatCard
        icon={<ActivityIcon size={24} />}
        tone="green"
        label={t("servers.list.stats.online")}
        value={online}
        action={{ pressed: filter === "online", onClick: () => onFilter("online") }}
      />
      <StatCard
        icon={<ActivityIcon size={24} />}
        tone="red"
        label={t("servers.list.stats.offline")}
        value={offline}
        action={{ pressed: filter === "offline", onClick: () => onFilter("offline") }}
      />
      <StatCard
        icon={<UsersIcon size={24} />}
        tone="violet"
        label={t("servers.list.stats.clients")}
        value={clients ?? "—"}
      />
    </div>
  );
}

function ServerTable({
  servers,
  metrics,
  sort,
  onSort,
}: {
  servers: Server[];
  metrics: Map<string, LatestMetrics>;
  sort: Sort<ServerSort>;
  onSort: (field: ServerSort) => void;
}) {
  const { t } = useTranslation();
  const rows = sort
    ? sortRows(servers, sort.direction, (server) => {
        if (sort.field === "name") return server.name;
        if (sort.field === "status") return serverRank(server);
        return metrics.get(server.id)?.clients_online;
      })
    : servers;
  return (
    <div className={table.wrap}>
      <table className={`${table.table} ${table.stack}`}>
        <thead>
          <tr>
            <SortHeader field="status" sort={sort} onSort={onSort}>
              {t("servers.list.columns.state")}
            </SortHeader>
            <SortHeader field="name" sort={sort} onSort={onSort}>
              {t("servers.list.columns.server")}
            </SortHeader>
            <th scope="col">{t("servers.list.columns.address")}</th>
            <SortHeader field="clients" sort={sort} onSort={onSort}>
              {t("servers.list.columns.clients")}
            </SortHeader>
            <th scope="col">{t("servers.list.columns.cpu")}</th>
            <th scope="col">{t("servers.list.columns.check")}</th>
            <th scope="col">{t("servers.list.columns.hostKey")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((server) => {
            const { signal, label } = serverSignal(server);
            const metric = metrics.get(server.id);
            const { flag, rest } = splitFlag(server.name);
            const cpu = metric?.cpu_percent;
            return (
              <tr key={server.id} data-signal={signal}>
                <td data-label={t("servers.list.columns.state")}>
                  <StatusPill tone={SIGNAL_TONE[signal]}>{label}</StatusPill>
                </td>
                <th scope="row" className={table.primary}>
                  <span className={styles.name}>
                    {flag && (
                      <span className={styles.flag} aria-hidden="true">
                        {flag}
                      </span>
                    )}
                    <Link to={`/servers/${server.id}`} className={table.title}>
                      {rest || server.name}
                    </Link>
                  </span>
                </th>
                <td className={table.mono} data-label={t("servers.list.columns.address")}>
                  {server.host}
                  {server.ssh_port !== 22 && `:${server.ssh_port}`}
                </td>
                <td
                  className={table.num}
                  data-label={t("servers.list.columns.clients")}
                  title={t("servers.list.clientsTitle")}
                >
                  {metric?.clients_online === null || metric?.clients_online === undefined
                    ? "—"
                    : t("servers.list.clientsOf", {
                        online: metric.clients_online,
                        total: metric.clients_total ?? 0,
                      })}
                </td>
                <td className={table.num} data-label={t("servers.list.columns.cpu")}>
                  <span className={styles.load}>
                    <span>{formatPercent(cpu)}</span>
                    {cpu !== null && cpu !== undefined && (
                      <span className={styles.meter} aria-hidden="true">
                        <span
                          className={styles.meterFill}
                          data-level={cpu >= 85 ? "high" : cpu >= 60 ? "mid" : "low"}
                          style={{ width: `${Math.min(100, Math.max(2, cpu))}%` }}
                        />
                      </span>
                    )}
                  </span>
                </td>
                <td className={table.muted} data-label={t("servers.list.columns.check")}>
                  {server.last_health_at ? (
                    <time dateTime={server.last_health_at} title={exactTime(server.last_health_at)}>
                      {timeAgo(server.last_health_at)}
                    </time>
                  ) : (
                    t("servers.list.neverChecked")
                  )}
                </td>
                <td className={table.muted} data-label={t("servers.list.columns.hostKey")}>
                  {server.ssh_host_key_fingerprint ? (
                    <code className={styles.fingerprint} title={server.ssh_host_key_fingerprint}>
                      {server.ssh_host_key_fingerprint.slice(0, 18)}…
                    </code>
                  ) : (
                    <span className={styles.unpinned}>{t("servers.list.hostKeyUnpinned")}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
