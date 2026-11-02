import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { api, unwrap, type VPNUserListItem } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Avatar } from "../components/Avatar";
import { Button } from "../components/Button";
import table from "../components/DataTable.module.css";
import { SearchIcon } from "../components/icons";
import { Notice } from "../components/Notice";
import { PageHeader } from "../components/PageHeader";
import { StatusPill } from "../components/StatusPill";
import { TextField } from "../components/TextField";
import i18n from "../i18n";
import { currentLanguage } from "../i18n/format";
import { accessPill, accessSignal, daysLeft, formatDay } from "../lib/access";
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

export function UsersPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const [search, setSearch] = useState("");
  const users = useQuery({
    queryKey: ["vpn-users"],
    queryFn: () => unwrap(api.GET("/api/v1/vpn-users")),
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
            <UserTable users={shown} />
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

function UserTable({ users }: { users: VPNUserListItem[] }) {
  const { t } = useTranslation();
  // Сначала те, у кого доступ скоро кончится, затем по имени — так список работает как напоминание о продлении.
  const order = { warn: 0, ok: 1, process: 2, down: 3, idle: 4 };
  const rows = users
    .map((user) => ({ user, ...accessSignal(user.access ?? null) }))
    .sort(
      (a, b) =>
        order[a.signal] - order[b.signal] ||
        a.user.username.localeCompare(b.user.username, currentLanguage()),
    );

  return (
    <div className={table.wrap}>
      <table className={`${table.table} ${table.stack}`}>
        <thead>
          <tr>
            <th scope="col">{t("users.list.columns.user")}</th>
            <th scope="col">{t("users.list.columns.access")}</th>
            <th scope="col">{t("users.list.columns.until")}</th>
            <th scope="col">{t("users.list.columns.note")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ user, signal }) => {
            const access = user.access ?? null;
            const pill = accessPill(access);
            const left = daysLeft(access);
            return (
              <tr key={user.id} data-signal={signal}>
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
                <td className={`${table.muted} ${styles.note}`} data-label={t("users.list.columns.note")}>
                  {user.description}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
