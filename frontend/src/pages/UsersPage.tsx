import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { api, unwrap, type VPNUserListItem } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Button } from "../components/Button";
import { Notice } from "../components/Notice";
import { accessSignal } from "../lib/access";
import { pick } from "../lib/serverSignal";
import styles from "./Ledger.module.css";
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
    counts.warn && `${counts.warn} кончается скоро`,
    counts.live && `${counts.live} с доступом`,
    counts.none && `${counts.none} без доступа`,
  ].filter(Boolean);
  const total = `${users.length} ${pick(users.length, ["пользователь", "пользователя", "пользователей"])}`;
  return `${total} — ${parts.join(", ")}`;
}

export function UsersPage() {
  const { can } = useAuth();
  const users = useQuery({
    queryKey: ["vpn-users"],
    queryFn: () => unwrap(api.GET("/api/v1/vpn-users")),
  });

  return (
    <section aria-labelledby="users-title">
      <header className={styles.header}>
        <h1 id="users-title" className={styles.title}>
          Пользователи
        </h1>
        {users.data && users.data.length > 0 && <p className={styles.summary}>{usersSummary(users.data)}</p>}
        {can("vpn_users:manage") && (
          <div className={styles.headerAction}>
            <AddUserDialog />
          </div>
        )}
      </header>

      {users.isPending && <p className={styles.quiet}>Загружаем пользователей…</p>}

      {users.isError && (
        <Notice
          action={
            <Button variant="quiet" onClick={() => void users.refetch()}>
              Повторить
            </Button>
          }
        >
          {users.error.message}
        </Notice>
      )}

      {users.data?.length === 0 && (
        <p className={styles.quiet}>
          Пользователей пока нет. Добавьте человека, которому нужен VPN, — доступ и конфиг выдаются на его
          странице.
        </p>
      )}

      {users.data && users.data.length > 0 && <UserTable users={users.data} />}
    </section>
  );
}

function UserTable({ users }: { users: VPNUserListItem[] }) {
  // Сначала те, у кого доступ скоро кончится, затем по имени — так список работает как напоминание о продлении.
  const order = { warn: 0, ok: 1, process: 2, down: 3, idle: 4 };
  const rows = users
    .map((user) => ({ user, ...accessSignal(user.access ?? null) }))
    .sort(
      (a, b) => order[a.signal] - order[b.signal] || a.user.username.localeCompare(b.user.username, "ru"),
    );

  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th scope="col">Пользователь</th>
          <th scope="col">Доступ</th>
          <th scope="col">Заметка</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(({ user, signal, label }) => (
          <tr key={user.id} className={styles.row} data-signal={signal}>
            <th scope="row" className={styles.name}>
              <Link to={`/users/${user.id}`} className={styles.link}>
                {user.username}
              </Link>
              <span className={styles.address}>{user.email}</span>
            </th>
            <td>
              <span className={styles.state} data-signal={signal}>
                {label}
              </span>
            </td>
            <td className={styles.meta}>{user.description}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
