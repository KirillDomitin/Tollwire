import { useTranslation } from "react-i18next";

import type { Job, Protocol, Server } from "../../../api/client";
import { useAuth } from "../../../auth/AuthContext";
import { Notice } from "../../../components/Notice";
import { isUnfinished } from "../../../lib/jobs";
import { exactTime, timeAgo } from "../../../lib/time";
import { AssignmentsPanel } from "./AssignmentsPanel";
import { ExitsPanel } from "./ExitsPanel";
import { GATEWAY_SUBNET, type GatewayExit } from "./gateway";
import styles from "./Gateway.module.css";
import { RuPanel } from "./RuPanel";
import { useAssignments, useExits, useGatewayClients } from "./useGateway";

const hasRunning = (jobs: Job[], type: Job["type"]) =>
  jobs.some((job) => job.type === type && isUnfinished(job));

/** Проверка контейнера из отчёта последней проверки состояния: отчёт читается из журнала задач, отдельного API нет. */
function containerCheck(jobs: Job[]): { ok: boolean; at: string } | null {
  const job = jobs.find((item) => item.type === "health_check" && item.status === "success");
  const checks = job?.result?.checks as { name: string; ok: boolean }[] | undefined;
  const found = checks?.find((check) => check.name === "container");
  return job && found ? { ok: found.ok, at: job.created_at } : null;
}

/** Выход годится для новых клиентов: действует, и его не считают отказавшим. */
const usable = (exit: GatewayExit) =>
  exit.state === "active" && exit.health !== "down" && exit.health !== "stale";

/**
 * Раздел «Шлюз»: сводка, выходы, клиенты и их выходы, «Россия напрямую». Показывается на сервере, где стоит протокол
 * шлюза; кнопки изменений — по правам (выходы и список: servers:manage, перенос клиентов: access:manage, проверка: servers:check).
 */
export function GatewaySection({
  server,
  protocol,
  jobs,
}: {
  server: Server;
  protocol: Protocol;
  jobs: Job[];
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const exits = useExits(server.id);
  const assignments = useAssignments(server.id);
  const { clients, loading } = useGatewayClients(
    server.id,
    protocol.id,
    can("access:read") && can("vpn_users:read"),
  );
  const container = containerCheck(jobs);
  const exitList = exits.data ?? [];
  const assignmentList = assignments.data ?? [];
  const assigned = new Set(assignmentList.map((item) => item.credential_id));
  const stranded = [...clients.entries()].filter(([id, client]) => client.live && !assigned.has(id)).length;

  return (
    <section className={styles.section} aria-labelledby="gateway-title">
      <h2 id="gateway-title" className={styles.sectionTitle}>
        {t("gateway.title")}
      </h2>
      <p className={styles.hint}>{t("gateway.description")}</p>

      <dl className={styles.facts}>
        <dt>{t("gateway.summary.subnet")}</dt>
        <dd>
          <code className={styles.keyValue}>{GATEWAY_SUBNET}</code>
        </dd>

        <dt>{t("gateway.summary.port")}</dt>
        <dd>
          {protocol.port
            ? t("servers.maintenance.port", { port: protocol.port })
            : t("servers.maintenance.portUnknown")}
        </dd>

        <dt>{t("gateway.summary.container")}</dt>
        <dd>
          {container ? (
            <>
              {container.ok ? t("gateway.summary.containerOk") : t("gateway.summary.containerBad")}
              <span className={styles.hint}>
                {t("gateway.summary.checked")}{" "}
                <time dateTime={container.at} title={exactTime(container.at)}>
                  {timeAgo(container.at)}
                </time>
              </span>
            </>
          ) : (
            <span className={styles.hint}>{t("gateway.summary.containerUnknown")}</span>
          )}
        </dd>

        <dt>{t("gateway.summary.exits")}</dt>
        <dd>
          {exits.data
            ? t("gateway.summary.exitsValue", {
                usable: exitList.filter(usable).length,
                total: exitList.length,
              })
            : "—"}
        </dd>

        <dt>{t("gateway.summary.clients")}</dt>
        <dd>
          {assignments.data ? t("gateway.summary.clientsValue", { count: assignmentList.length }) : "—"}
          {stranded > 0 && (
            <span className={styles.badge} data-tone="down">
              {t("gateway.summary.stranded", { count: stranded })}
            </span>
          )}
        </dd>
      </dl>

      {exits.isError && <Notice>{exits.error.message}</Notice>}
      {assignments.isError && <Notice>{assignments.error.message}</Notice>}
      {exits.data && exitList.length === 0 && (
        <p className={styles.warning} data-tone="down" role="status">
          {t("gateway.warnings.noExits")}
        </p>
      )}
      {exits.data && exitList.length > 0 && !exitList.some(usable) && (
        <p className={styles.warning} data-tone="down" role="status">
          {t("gateway.warnings.noUsable")}
        </p>
      )}

      {exits.isPending ? (
        <p className={styles.hint}>{t("gateway.loading")}</p>
      ) : (
        exits.data && (
          <>
            <ExitsPanel
              serverId={server.id}
              exits={exitList}
              manage={can("servers:manage")}
              adding={hasRunning(jobs, "gateway_add_exit")}
              removing={hasRunning(jobs, "gateway_remove_exit")}
            />
            <AssignmentsPanel
              serverId={server.id}
              exits={exitList}
              assignments={assignmentList}
              clients={clients}
              clientsLoading={loading || assignments.isPending}
              canMove={can("access:manage")}
              canCheck={can("servers:check")}
              checking={hasRunning(jobs, "gateway_apply")}
            />
          </>
        )
      )}
      <RuPanel serverId={server.id} manage={can("servers:manage")} jobs={jobs} />
    </section>
  );
}
