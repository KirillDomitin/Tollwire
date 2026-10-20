import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { Button } from "../../../components/Button";
import { ConfirmDialog } from "../../../components/ConfirmDialog";
import { Notice } from "../../../components/Notice";
import { exactTime, timeAgo } from "../../../lib/time";
import { AddExitDialog } from "./AddExitDialog";
import { EditExitDialog } from "./EditExitDialog";
import { exitView, healthLabel, type GatewayExit } from "./gateway";
import styles from "./Gateway.module.css";
import { useAddExit, useRemoveExit } from "./useGatewayActions";

/** Выходы шлюза: куда уходит трафик клиентов. Изменения — при servers:manage. */
export function ExitsPanel({
  serverId,
  exits,
  manage,
  adding,
  removing,
}: {
  serverId: string;
  exits: GatewayExit[];
  manage: boolean;
  /** Идёт задача добавления выхода. */
  adding: boolean;
  /** Идёт задача снятия выхода. */
  removing: boolean;
}) {
  const { t } = useTranslation();
  const [addOpen, setAddOpen] = useState(false);
  const retry = useAddExit(serverId);

  return (
    <div className={styles.block}>
      <div className={styles.blockHead}>
        <h3 className={styles.blockTitle}>{t("gateway.exits.title")}</h3>
        {manage && (
          <Button onClick={() => setAddOpen(true)} busy={adding} busyLabel={t("gateway.exits.adding")}>
            {t("gateway.exits.add.trigger")}
          </Button>
        )}
      </div>

      {exits.length === 0 ? (
        <p className={styles.hint}>{t("gateway.exits.empty")}</p>
      ) : (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col">{t("gateway.exits.columns.exit")}</th>
                <th scope="col">{t("gateway.exits.columns.state")}</th>
                <th scope="col">{t("gateway.exits.columns.health")}</th>
                <th scope="col">{t("gateway.exits.columns.weight")}</th>
                <th scope="col">{t("gateway.exits.columns.limit")}</th>
                <th scope="col">{t("gateway.exits.columns.clients")}</th>
                {manage && (
                  <th scope="col">
                    <span className="visually-hidden">{t("gateway.exits.columns.actions")}</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {exits.map((exit) => {
                const view = exitView(exit, { adding, removing });
                return (
                  <tr key={exit.id} data-tone={view.tone}>
                    <th scope="row" className={styles.name}>
                      <Link to={`/servers/${exit.exit_server_id}`} className={styles.link}>
                        {exit.exit_server_name}
                      </Link>
                      <span className={styles.sub}>{t("gateway.exits.slot", { slot: exit.slot })}</span>
                    </th>
                    <td data-label={t("gateway.exits.columns.state")}>
                      <span className={styles.badge} data-tone={view.tone}>
                        {t(`gateway.exits.states.${view.state}`)}
                      </span>
                    </td>
                    <td data-label={t("gateway.exits.columns.health")}>
                      <span className={styles.stack}>
                        {exit.state === "active" ? healthLabel(exit.health) : "—"}
                        {exit.state === "active" && exit.last_handshake_at && (
                          <span className={styles.sub}>
                            {t("gateway.exits.handshake")}{" "}
                            <time dateTime={exit.last_handshake_at} title={exactTime(exit.last_handshake_at)}>
                              {timeAgo(exit.last_handshake_at)}
                            </time>
                          </span>
                        )}
                      </span>
                    </td>
                    <td data-label={t("gateway.exits.columns.weight")} className={styles.num}>
                      {exit.weight}
                    </td>
                    <td data-label={t("gateway.exits.columns.limit")} className={styles.num}>
                      {exit.max_clients ?? t("gateway.exits.unlimited")}
                    </td>
                    <td data-label={t("gateway.exits.columns.clients")} className={styles.num}>
                      {exit.assigned_clients}
                    </td>
                    {manage && (
                      <td data-actions>
                        <div className={styles.actions}>
                          {view.stalled === "add" ? (
                            <Button
                              variant="quiet"
                              busy={retry.isPending}
                              busyLabel={t("servers.card.queueBusy")}
                              onClick={() =>
                                retry.mutate({
                                  exit_server_id: exit.exit_server_id,
                                  weight: exit.weight,
                                  max_clients: exit.max_clients,
                                })
                              }
                            >
                              {t("gateway.exits.retry")}
                            </Button>
                          ) : (
                            view.stalled === null && <EditExitDialog serverId={serverId} exit={exit} />
                          )}
                          {!(view.state === "removing") && (
                            <RemoveExitButton serverId={serverId} exit={exit} />
                          )}
                        </div>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {retry.isError && <Notice>{retry.error.message}</Notice>}

      {manage && (
        <AddExitDialog
          serverId={serverId}
          connected={exits.map((exit) => exit.exit_server_id)}
          open={addOpen}
          onOpenChange={setAddOpen}
        />
      )}
    </div>
  );
}

/** Снять выход: клиенты переедут на другие выходы; если им некуда деться, диалог скажет, сколько останется без выхода. */
function RemoveExitButton({ serverId, exit }: { serverId: string; exit: GatewayExit }) {
  const { t } = useTranslation();
  const remove = useRemoveExit(serverId, exit.id);
  return (
    <ConfirmDialog
      trigger={
        <Button
          variant="quiet"
          aria-label={t("gateway.exits.remove.trigger", { name: exit.exit_server_name })}
        >
          {t("gateway.exits.remove.button")}
        </Button>
      }
      title={t("gateway.exits.remove.title", { name: exit.exit_server_name })}
      consequences={t("gateway.exits.remove.consequences", { count: exit.assigned_clients })}
      actionLabel={t("gateway.exits.remove.action")}
      busyLabel={t("servers.card.queueBusy")}
      onConfirm={(again) => remove(again)}
    />
  );
}
