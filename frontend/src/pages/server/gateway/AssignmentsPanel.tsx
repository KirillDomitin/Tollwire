import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { Button } from "../../../components/Button";
import { Notice } from "../../../components/Notice";
import { exactTime, timeAgo } from "../../../lib/time";
import { PAGE_SIZE, reasonLabel, type GatewayAssignment, type GatewayExit } from "./gateway";
import styles from "./Gateway.module.css";
import { MoveClientDialog } from "./MoveClientDialog";
import { useCheckNow, useMoveClient } from "./useGatewayActions";
import type { GatewayClient } from "./useGateway";

interface Row {
  credentialId: string;
  client: GatewayClient | undefined;
  assignment: GatewayAssignment | null;
}

/**
 * Какой клиент через какой выход идёт. Строки без назначения — клиенты, у которых нет выхода (и интернета): их видно
 * только отсюда, бэкенд отдельного статуса не ведёт. Перенос и закрепление — при access:manage.
 */
export function AssignmentsPanel({
  serverId,
  exits,
  assignments,
  clients,
  clientsLoading,
  canMove,
  canCheck,
  checking,
}: {
  serverId: string;
  exits: GatewayExit[];
  assignments: GatewayAssignment[];
  clients: Map<string, GatewayClient>;
  clientsLoading: boolean;
  canMove: boolean;
  canCheck: boolean;
  /** Идёт задача проверки выходов. */
  checking: boolean;
}) {
  const { t } = useTranslation();
  const [shown, setShown] = useState(PAGE_SIZE);
  const check = useCheckNow(serverId);
  const exitById = new Map(exits.map((exit) => [exit.id, exit]));

  const assigned = new Set(assignments.map((assignment) => assignment.credential_id));
  const stranded: Row[] = [...clients.entries()]
    .filter(([id, client]) => client.live && !assigned.has(id))
    .map(([credentialId, client]) => ({ credentialId, client, assignment: null }));
  const withExit: Row[] = assignments.map((assignment) => ({
    credentialId: assignment.credential_id,
    client: clients.get(assignment.credential_id),
    assignment,
  }));
  const byName = (row: Row) => row.client?.username ?? "";
  const rows = [
    ...stranded.sort((a, b) => byName(a).localeCompare(byName(b))),
    ...withExit.sort((a, b) => a.assignment!.slot - b.assignment!.slot || byName(a).localeCompare(byName(b))),
  ];

  return (
    <div className={styles.block}>
      <div className={styles.blockHead}>
        <h3 className={styles.blockTitle}>{t("gateway.assignments.title")}</h3>
        {canCheck && (
          <Button
            variant="quiet"
            busy={checking || check.isPending}
            busyLabel={t("gateway.assignments.checking")}
            onClick={() => check.mutate()}
          >
            {t("gateway.assignments.check")}
          </Button>
        )}
      </div>
      <p className={styles.hint}>{t("gateway.assignments.description")}</p>
      {check.isError && <Notice>{check.error.message}</Notice>}

      {stranded.length > 0 && (
        <p className={styles.warning} data-tone="down" role="status">
          {t("gateway.assignments.stranded", { count: stranded.length })}
        </p>
      )}

      {rows.length === 0 ? (
        <p className={styles.hint}>
          {clientsLoading ? t("gateway.assignments.loading") : t("gateway.assignments.empty")}
        </p>
      ) : (
        <>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">{t("gateway.assignments.columns.client")}</th>
                  <th scope="col">{t("gateway.assignments.columns.exit")}</th>
                  <th scope="col">{t("gateway.assignments.columns.reason")}</th>
                  <th scope="col">{t("gateway.assignments.columns.pinned")}</th>
                  {canMove && (
                    <th scope="col">
                      <span className="visually-hidden">{t("gateway.exits.columns.actions")}</span>
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, shown).map((row) => (
                  <ClientRow
                    key={row.credentialId}
                    serverId={serverId}
                    row={row}
                    exits={exits}
                    exitById={exitById}
                    canMove={canMove}
                  />
                ))}
              </tbody>
            </table>
          </div>
          {rows.length > shown && (
            <div className={styles.row}>
              <Button variant="quiet" onClick={() => setShown(shown + PAGE_SIZE)}>
                {t("gateway.assignments.more", { shown: Math.min(shown, rows.length), total: rows.length })}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function ClientRow({
  serverId,
  row,
  exits,
  exitById,
  canMove,
}: {
  serverId: string;
  row: Row;
  exits: GatewayExit[];
  exitById: Map<string, GatewayExit>;
  canMove: boolean;
}) {
  const { t } = useTranslation();
  const pinning = useMoveClient(serverId, row.credentialId);
  const { assignment, client } = row;
  const name = client?.username ?? t("gateway.assignments.unnamed", { id: row.credentialId.slice(0, 8) });
  const exit = assignment ? exitById.get(assignment.exit_id) : undefined;
  const pinnedTo = assignment?.pinned_exit_id ? exitById.get(assignment.pinned_exit_id) : undefined;

  return (
    <tr data-tone={assignment ? "ok" : "down"}>
      <th scope="row" className={styles.name}>
        {client ? (
          <Link to={`/users/${client.userId}`} className={styles.link}>
            {name}
          </Link>
        ) : (
          name
        )}
        {assignment && (
          <span className={styles.sub}>
            {t("gateway.assignments.since")}{" "}
            <time dateTime={assignment.assigned_at} title={exactTime(assignment.assigned_at)}>
              {timeAgo(assignment.assigned_at)}
            </time>
          </span>
        )}
      </th>
      <td data-label={t("gateway.assignments.columns.exit")}>
        {assignment ? (
          <span>
            {t("gateway.assignments.exitOption", {
              slot: assignment.slot,
              name: exit?.exit_server_name ?? t("gateway.assignments.exitGone"),
            })}
          </span>
        ) : (
          <span className={styles.badge} data-tone="down">
            {t("gateway.assignments.noExit")}
          </span>
        )}
      </td>
      <td data-label={t("gateway.assignments.columns.reason")}>
        {assignment ? reasonLabel(assignment.reason) : "—"}
      </td>
      <td data-label={t("gateway.assignments.columns.pinned")}>
        {assignment?.pinned_exit_id ? (
          <span>
            {pinnedTo
              ? t("gateway.assignments.pinnedTo", { slot: pinnedTo.slot, name: pinnedTo.exit_server_name })
              : t("gateway.assignments.pinned")}
            {assignment.pinned_exit_id !== assignment.exit_id && (
              <span className={styles.sub}> · {t("gateway.assignments.pinnedElsewhere")}</span>
            )}
          </span>
        ) : (
          "—"
        )}
      </td>
      {canMove && (
        <td data-actions>
          <div className={styles.actions}>
            <MoveClientDialog
              serverId={serverId}
              credentialId={row.credentialId}
              clientName={name}
              assignment={assignment}
              exits={exits}
            />
            {assignment && (
              <Button
                variant="quiet"
                busy={pinning.isPending}
                busyLabel={t("gateway.assignments.move.busy")}
                aria-label={t(
                  assignment.pinned_exit_id
                    ? "gateway.assignments.unpinTrigger"
                    : "gateway.assignments.pinTrigger",
                  { name },
                )}
                onClick={() => pinning.mutate({ exit_id: null, pin: !assignment.pinned_exit_id })}
              >
                {assignment.pinned_exit_id ? t("gateway.assignments.unpin") : t("gateway.assignments.pin")}
              </Button>
            )}
          </div>
          {pinning.isError && <Notice>{pinning.error.message}</Notice>}
        </td>
      )}
    </tr>
  );
}
