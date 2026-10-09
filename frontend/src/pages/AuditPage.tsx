import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";

import { api, unwrap } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Button } from "../components/Button";
import { Notice } from "../components/Notice";
import {
  actionGroups,
  actionsOfGroup,
  AUDIT_ACTIONS,
  auditActor,
  auditDetails,
  auditSubject,
  auditTone,
  type AuditLog,
  type Detail,
  type Names,
} from "../lib/audit";
import { exactTime, timeAgo } from "../lib/time";
import styles from "./AuditPage.module.css";
import ledger from "./Ledger.module.css";

const LIMIT = 100;

interface Filters {
  adminId: string;
  group: string;
  entityType: string;
  entityId: string;
  from: string; // YYYY-MM-DD
  to: string;
}

const PARAMS = {
  adminId: "admin_id",
  group: "group",
  entityType: "entity_type",
  entityId: "entity_id",
  from: "from",
  to: "to",
} as const satisfies Record<keyof Filters, string>;

/** Дата из поля ввода — начало этого дня по местному времени, со сдвигом на `days` дней. */
function localDay(value: string, days = 0): string | undefined {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return undefined;
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + days).toISOString();
}

interface Cursor {
  created_at: string;
  id: string;
}

export function AuditPage() {
  const { can } = useAuth();
  const allowed = can("audit:read");
  const [params, setParams] = useSearchParams();

  const filters: Filters = {
    adminId: params.get(PARAMS.adminId) ?? "",
    group: params.get(PARAMS.group) ?? "",
    entityType: params.get(PARAMS.entityType) ?? "",
    entityId: params.get(PARAMS.entityId) ?? "",
    from: params.get(PARAMS.from) ?? "",
    to: params.get(PARAMS.to) ?? "",
  };
  const filtered = Object.values(filters).some(Boolean);

  function setFilter(key: keyof Filters, value: string) {
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (value) next.set(PARAMS[key], value);
        else next.delete(PARAMS[key]);
        return next;
      },
      { replace: true },
    );
  }

  const actors = useQuery({
    queryKey: ["audit-actors"],
    queryFn: () => unwrap(api.GET("/api/v1/audit-logs/actors")),
    enabled: allowed,
  });
  // Имена для столбца «Объект»: без права на списки строка просто не называет объект.
  const servers = useQuery({
    queryKey: ["servers"],
    queryFn: () => unwrap(api.GET("/api/v1/servers")),
    enabled: allowed && can("servers:read"),
  });
  const users = useQuery({
    queryKey: ["vpn-users"],
    queryFn: () => unwrap(api.GET("/api/v1/vpn-users")),
    enabled: allowed && can("vpn_users:read"),
  });

  const log = useInfiniteQuery({
    queryKey: ["audit-logs", filters],
    enabled: allowed,
    initialPageParam: null as Cursor | null,
    queryFn: ({ pageParam }) => {
      const actions = filters.group ? actionsOfGroup(filters.group) : [];
      return unwrap(
        api.GET("/api/v1/audit-logs", {
          params: {
            query: {
              limit: LIMIT,
              admin_id: filters.adminId || undefined,
              action: actions.length > 0 ? actions : undefined,
              entity_type: filters.entityType || undefined,
              entity_id: filters.entityId || undefined,
              since: localDay(filters.from),
              until: localDay(filters.to, 1), // «по» включает весь день, а until границу не включает
              before_created_at: pageParam?.created_at,
              before_id: pageParam?.id,
            },
          },
        }),
      );
    },
    getNextPageParam: (last) => {
      const tail = last.at(-1);
      return last.length === LIMIT && tail ? { created_at: tail.created_at, id: tail.id } : undefined;
    },
  });

  if (!allowed) {
    return <Notice>Журнал аудита доступен только админам с правом чтения аудита.</Notice>;
  }

  const names: Names = {
    servers: new Map((servers.data ?? []).map((server) => [server.id, server.name])),
    users: new Map((users.data ?? []).map((user) => [user.id, user.username])),
    admins: new Map((actors.data ?? []).map((actor) => [actor.id, actor.email])),
  };
  const rows = log.data?.pages.flat() ?? [];
  const now = new Date(log.dataUpdatedAt);

  return (
    <section aria-labelledby="audit-title">
      <header className={ledger.header}>
        <h1 id="audit-title" className={ledger.title}>
          Журнал аудита
        </h1>
      </header>

      <form className={styles.filters} onSubmit={(event) => event.preventDefault()}>
        <label className={styles.field}>
          <span className={styles.label}>Админ</span>
          <select
            className={styles.control}
            value={filters.adminId}
            onChange={(event) => setFilter("adminId", event.target.value)}
          >
            <option value="">Все</option>
            {(actors.data ?? []).map((actor) => (
              <option key={actor.id} value={actor.id}>
                {actor.email}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span className={styles.label}>Группа</span>
          <select
            className={styles.control}
            value={filters.group}
            onChange={(event) => setFilter("group", event.target.value)}
          >
            <option value="">Все события</option>
            {actionGroups().map(([group]) => (
              <option key={group} value={group}>
                {group}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span className={styles.label}>С даты</span>
          <input
            type="date"
            className={styles.control}
            value={filters.from}
            max={filters.to || undefined}
            onChange={(event) => setFilter("from", event.target.value)}
          />
        </label>
        <label className={styles.field}>
          <span className={styles.label}>По дату</span>
          <input
            type="date"
            className={styles.control}
            value={filters.to}
            min={filters.from || undefined}
            onChange={(event) => setFilter("to", event.target.value)}
          />
        </label>
        {filtered && (
          <Button variant="quiet" onClick={() => setParams({}, { replace: true })}>
            Сбросить
          </Button>
        )}
      </form>

      {log.isPending && <p className={ledger.quiet}>Загружаем журнал…</p>}

      {log.isError && (
        <Notice
          action={
            <Button variant="quiet" onClick={() => void log.refetch()}>
              Повторить
            </Button>
          }
        >
          {log.error.message}
        </Notice>
      )}

      {log.data && rows.length === 0 && (
        <p className={ledger.quiet}>{filtered ? "Под фильтры ничего не попало" : "Событий пока нет"}</p>
      )}

      {rows.length > 0 && (
        <>
          <p className={styles.count} aria-live="polite">
            Показано событий: {rows.length}
          </p>
          <table className={ledger.table} aria-busy={log.isFetchingNextPage}>
            <thead>
              <tr>
                <th scope="col">Событие</th>
                <th scope="col">Кто</th>
                <th scope="col">Объект</th>
                <th scope="col">Когда</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((entry) => (
                <Row key={entry.id} entry={entry} names={names} now={now} />
              ))}
            </tbody>
          </table>
          {log.hasNextPage && (
            <div className={styles.pager}>
              <Button
                variant="quiet"
                busy={log.isFetchingNextPage}
                busyLabel="Загружаем…"
                onClick={() => void log.fetchNextPage()}
              >
                Показать ещё
              </Button>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function Row({ entry, names, now }: { entry: AuditLog; names: Names; now: Date }) {
  const tone = auditTone(entry.action);
  const subject = auditSubject(entry, names);
  const details: Detail[] = auditDetails(entry);
  if (entry.ip_address) details.push({ label: "Адрес", value: entry.ip_address });

  return (
    <tr className={ledger.row} data-signal={tone ?? "idle"}>
      <th scope="row" className={styles.event} data-tone={tone ?? undefined}>
        <span className={styles.text}>{AUDIT_ACTIONS[entry.action].text}</span>
        {details.length > 0 && (
          <details className={styles.details}>
            <summary>Подробности</summary>
            <dl className={styles.facts}>
              {details.map((detail, index) => (
                <div key={index} className={styles.fact}>
                  <dt>{detail.label}</dt>
                  <dd>{detail.value}</dd>
                </div>
              ))}
            </dl>
          </details>
        )}
      </th>
      <td className={ledger.meta}>{auditActor(entry)}</td>
      <td>
        {subject ? (
          subject.to ? (
            <Link to={subject.to} className={ledger.link}>
              {subject.label}
            </Link>
          ) : (
            subject.label
          )
        ) : (
          "—"
        )}
      </td>
      <td className={ledger.meta}>
        <time dateTime={entry.created_at} title={exactTime(entry.created_at)}>
          {timeAgo(entry.created_at, now)}
        </time>
      </td>
    </tr>
  );
}
