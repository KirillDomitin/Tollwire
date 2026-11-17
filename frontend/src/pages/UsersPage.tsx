import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { api, unwrap, type VPNUserListItem } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Avatar } from "../components/Avatar";
import { Button } from "../components/Button";
import table from "../components/DataTable.module.css";
import { SortHeader } from "../components/SortHeader";
import { accessRank, nextSort, sortRows, type Sort } from "../lib/tableSort";
import { SearchIcon } from "../components/icons";
import { Notice } from "../components/Notice";
import { PageHeader } from "../components/PageHeader";
import { StatusPill } from "../components/StatusPill";
import { TextField } from "../components/TextField";
import i18n from "../i18n";
import { currentLanguage } from "../i18n/format";
import { accessPill, accessSignal, daysLeft, formatDay } from "../lib/access";
import { exactTime, timeAgo } from "../lib/time";
import { formatBytes } from "../lib/units";
import styles from "./UsersPage.module.css";
import { AddUserDialog } from "./user/AddUserDialog";

/** Сводка одной фразой, сначала то, что требует внимания: «5 пользователей — 1 кончается скоро, 3 с доступом». */
export function usersSummary(users: VPNUserListItem[], now: Date = new Date()): string {
  const counts = { warn: 0, live: 0, none: 0 };
  for (const user of users) {
    const { signal } = accessSignal(user.access ?? null, now);
    if (signal === "warn") counts.warn += 1;
    else if (signal === "ok" || signal === "process") counts.live += 1;
    else counts.none += 1;
  }
  const parts = [
    counts.warn && i18n.t("users.summary.expiring", { count: counts.warn }),
    counts.live && i18n.t("users.summary.withAccess", { count: counts.live }),
    counts.none && i18n.t("users.summary.withoutAccess", { count: counts.none }),
  ].filter(Boolean);
  const total = i18n.t("users.summary.total", { count: users.length });
  return `${total} — ${parts.join(", ")}`;
}

type UserSort = "name" | "access" | "until" | "handshake" | "traffic";
type Device = NonNullable<VPNUserListItem["devices"]>[number];

function deviceTraffic(device: Device): number | null {
  const { downloaded_bytes, uploaded_bytes } = device;
  if (downloaded_bytes === null && uploaded_bytes === null) return null;
  return (downloaded_bytes ?? 0) + (uploaded_bytes ?? 0);
}

function userActivity(user: VPNUserListItem) {
  const devices = user.devices ?? [];
  let traffic: number | null = null;
  let latest: Device | undefined;
  for (const device of devices) {
    const bytes = deviceTraffic(device);
    if (bytes !== null) traffic = (traffic ?? 0) + bytes;
    if (
      device.latest_handshake &&
      (!latest?.latest_handshake || Date.parse(device.latest_handshake) > Date.parse(latest.latest_handshake))
    ) {
      latest = device;
    }
  }
  return { devices, traffic, latest };
}

export function UsersPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<Sort<UserSort>>(null);
  const users = useQuery({
    queryKey: ["vpn-users", "devices"],
    queryFn: () => unwrap(api.GET("/api/v1/vpn-users", { params: { query: { include_devices: true } } })),
    refetchInterval: 60_000,
  });
  const all = users.data ?? [];
  const query = search.trim().toLowerCase();
  const shown = query
    ? all.filter((user) => `${user.username} ${user.email ?? ""}`.toLowerCase().includes(query))
    : all;

  return (
    <section aria-labelledby="users-title">
      <PageHeader
        id="users-title"
        title={t("users.list.title")}
        subtitle={t("users.list.subtitle")}
        actions={can("vpn_users:manage") ? <AddUserDialog /> : undefined}
      />

      {users.isPending && <p className={styles.quiet}>{t("users.list.loading")}</p>}

      {users.isError && (
        <Notice
          action={
            <Button variant="secondary" onClick={() => void users.refetch()}>
              {t("users.list.retry")}
            </Button>
          }
        >
          {users.error.message}
        </Notice>
      )}

      {users.data?.length === 0 && <p className={styles.quiet}>{t("users.list.empty")}</p>}

      {all.length > 0 && (
        <>
          <div className={styles.toolbar}>
            <TextField
              className={styles.search}
              type="search"
              label={t("users.list.search")}
              hideLabel
              placeholder={t("users.list.search")}
              icon={<SearchIcon />}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              autoComplete="off"
            />
          </div>
          {shown.length === 0 ? (
            <p className={styles.quiet}>{t("users.list.noMatches")}</p>
          ) : (
            <UserTable users={shown} sort={sort} onSort={(field) => setSort(nextSort(sort, field))} />
          )}
          <div className={`${table.foot} ${styles.foot}`}>
            <span>{t("users.list.shown", { shown: shown.length, total: all.length })}</span>
            <span>{usersSummary(all)}</span>
          </div>
        </>
      )}
    </section>
  );
}

function UserTable({
  users,
  sort,
  onSort,
}: {
  users: VPNUserListItem[];
  sort: Sort<UserSort>;
  onSort: (field: UserSort) => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  // Сначала те, у кого доступ скоро кончится, затем по имени — так список работает как напоминание о продлении.
  const order = { warn: 0, ok: 1, process: 2, down: 3, idle: 4 };
  const now = new Date();
  const entries = users.map((user) => ({ id: user.id, user, ...userActivity(user) }));
  const sorted = sort
    ? sortRows(entries, sort.direction, ({ user, latest, traffic }) => {
        if (sort.field === "name") return user.username;
        if (sort.field === "access") return accessRank(user, now);
        if (sort.field === "handshake") return latest?.latest_handshake ? Date.parse(latest.latest_handshake) : null;
        if (sort.field === "traffic") return traffic;
        return user.access && user.access.status !== "revoked" ? Date.parse(user.access.valid_until) : null;
      })
    : entries;
  const rows = sorted
    .map((entry) => ({ ...entry, ...accessSignal(entry.user.access ?? null, now) }))
    .sort((a, b) =>
      sort
        ? 0
        : order[a.signal] - order[b.signal] ||
          a.user.username.localeCompare(b.user.username, currentLanguage()),
    );

  return (
    <div className={table.wrap}>
      <table className={`${table.table} ${table.stack}`}>
        <thead>
          <tr>
            <SortHeader field="name" sort={sort} onSort={onSort}>
              {t("users.list.columns.user")}
            </SortHeader>
            <SortHeader field="access" sort={sort} onSort={onSort}>
              {t("users.list.columns.access")}
            </SortHeader>
            <SortHeader field="until" sort={sort} onSort={onSort}>
              {t("users.list.columns.until")}
            </SortHeader>
            <SortHeader field="handshake" sort={sort} onSort={onSort}>
              {t("users.metrics.handshake")}
            </SortHeader>
            <SortHeader field="traffic" sort={sort} onSort={onSort}>
              {t("users.list.columns.traffic")}
            </SortHeader>
            <th scope="col">{t("users.list.columns.note")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ user, signal, devices, traffic, latest }) => {
            const access = user.access ?? null;
            const pill = accessPill(access);
            const left = daysLeft(access);
            const isExpanded = expanded.has(user.id);
            const detailsId = `user-devices-${user.id}`;
            const partial = traffic !== null && devices.some(
              (device) => device.downloaded_bytes === null || device.uploaded_bytes === null,
            );
            const staleTraffic = devices.some((device) => device.stale && deviceTraffic(device) !== null);
            return (
              <Fragment key={user.id}>
                <tr data-signal={signal}>
                  <th scope="row" className={table.primary}>
                    <span className={styles.person}>
                      <Avatar name={user.username} />
                      <span className={styles.who}>
                        <Link to={`/users/${user.id}`} className={table.title}>
                          {user.username}
                        </Link>
                        <span className={styles.email}>{user.email ?? "—"}</span>
                      </span>
                      {user.status === "disabled" && (
                        <StatusPill tone="neutral">{t("users.list.disabled")}</StatusPill>
                      )}
                    </span>
                  </th>
                  <td data-label={t("users.list.columns.access")}>
                    <StatusPill tone={pill.tone}>{pill.label}</StatusPill>
                  </td>
                  <td className={table.num} data-label={t("users.list.columns.until")}>
                    {access && access.status !== "revoked" ? (
                      <span className={styles.until}>
                        <span>{formatDay(access.valid_until)}</span>
                        {left && <span className={table.muted}>{left}</span>}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td data-label={t("users.metrics.handshake")}>
                    {latest?.latest_handshake ? (
                      <span className={styles.activity}>
                        <time dateTime={latest.latest_handshake} title={exactTime(latest.latest_handshake)}>
                          {timeAgo(latest.latest_handshake, now)}
                        </time>
                        {latest.stale && <small>{t("users.metrics.stale")}</small>}
                      </span>
                    ) : "—"}
                  </td>
                  <td data-label={t("users.list.columns.traffic")}>
                    {devices.length > 0 ? (
                      <span className={styles.activity}>
                        <Button
                          variant="ghost"
                          size="sm"
                          className={styles.trafficButton}
                          aria-expanded={isExpanded}
                          aria-controls={isExpanded ? detailsId : undefined}
                          aria-label={t("users.list.deviceTraffic", { user: user.username, traffic: formatBytes(traffic) })}
                          title={t("users.metrics.period")}
                          onClick={() => setExpanded((previous) => {
                            const next = new Set(previous);
                            if (next.has(user.id)) next.delete(user.id);
                            else next.add(user.id);
                            return next;
                          })}
                        >
                          {formatBytes(traffic)} <span aria-hidden="true">{isExpanded ? "▴" : "▾"}</span>
                        </Button>
                        {partial && <small>{t("users.list.partialTraffic")}</small>}
                        {staleTraffic && <small>{t("users.metrics.stale")}</small>}
                      </span>
                    ) : "—"}
                  </td>
                  <td className={styles.note} data-label={t("users.list.columns.note")}>
                    {user.description}
                  </td>
                </tr>
                {isExpanded && devices.length > 0 && (
                  <tr>
                    <td colSpan={6}>
                      <ul id={detailsId} className={styles.devices} aria-label={t("users.list.devices")}>
                        {devices.map((device) => (
                          <li key={device.credential_id} className={styles.device}>
                            <span>
                              <strong>{device.device_name || t("users.devices.unnamed", { id: device.credential_id.slice(0, 8) })}</strong>
                              <span className={styles.server}>{device.server_name}</span>
                            </span>
                            <span className={styles.activity}>
                              {t("users.metrics.downloaded")} {formatBytes(device.downloaded_bytes)}
                              <span>{t("users.metrics.uploaded")} {formatBytes(device.uploaded_bytes)}</span>
                              {device.stale && <small>{t("users.metrics.stale")}</small>}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
