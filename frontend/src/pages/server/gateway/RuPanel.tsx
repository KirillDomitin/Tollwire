import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { Job } from "../../../api/client";
import { Button } from "../../../components/Button";
import { ConfirmDialog } from "../../../components/ConfirmDialog";
import { Notice } from "../../../components/Notice";
import { isUnfinished } from "../../../lib/jobs";
import { exactTime, timeAgo } from "../../../lib/time";
import { geoipKeys } from "../useServer";
import { AddOverrideDialog } from "./AddOverrideDialog";
import { ingestMessage, type GeoipList } from "./gateway";
import styles from "./Gateway.module.css";
import { UploadListDialog } from "./UploadListDialog";
import { useGeoip, useJob, useOverrides } from "./useGateway";
import { useActivateList, useApplyRu, useRemoveOverride, useUpdateList } from "./useGatewayActions";

/**
 * «Россия напрямую»: клиенты шлюза, идущие на российские адреса, выходят с адреса самого шлюза, остальное — через выход.
 * Список общий для всех шлюзов панели; применяется на шлюзе отдельно. Изменения — при servers:manage.
 */
export function RuPanel({ serverId, manage, jobs }: { serverId: string; manage: boolean; jobs: Job[] }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const geoip = useGeoip();
  const overrides = useOverrides();
  const update = useUpdateList();
  const apply = useApplyRu(serverId);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [uploaded, setUploaded] = useState<string | null>(null);
  const updateJob = useJob(update.data?.id ?? null);

  // Обновление списка идёт в фоне и в журнал сервера не попадает: по завершении перечитываем список и кандидатов.
  const updateDone = updateJob.data !== undefined && !isUnfinished(updateJob.data);
  useEffect(() => {
    if (updateDone) void queryClient.invalidateQueries({ queryKey: geoipKeys.all });
  }, [updateDone, queryClient]);

  const active = geoip.data?.active ?? null;
  const applying = jobs.some((job) => job.type === "gateway_apply_ru" && isUnfinished(job));
  const lastApply = jobs.find((job) => job.type === "gateway_apply_ru" && !isUnfinished(job));

  return (
    <div className={styles.block}>
      <div className={styles.blockHead}>
        <h3 className={styles.blockTitle}>{t("gateway.ru.title")}</h3>
        {manage && (
          <div className={styles.row}>
            <Button
              variant="quiet"
              busy={update.isPending || (updateJob.data !== undefined && !updateDone)}
              busyLabel={t("gateway.ru.updating")}
              onClick={() => {
                setUploaded(null);
                update.mutate();
              }}
            >
              {t("gateway.ru.update")}
            </Button>
            <Button variant="quiet" onClick={() => setUploadOpen(true)}>
              {t("gateway.ru.upload.trigger")}
            </Button>
            <Button
              disabled={active === null}
              busy={applying || apply.isPending}
              busyLabel={t("gateway.ru.applying")}
              onClick={() => apply.mutate()}
            >
              {t("gateway.ru.apply")}
            </Button>
          </div>
        )}
      </div>
      <p className={styles.hint}>{t("gateway.ru.description")}</p>

      {geoip.isError && <Notice>{geoip.error.message}</Notice>}
      {geoip.isPending && <p className={styles.hint}>{t("gateway.ru.loading")}</p>}
      {geoip.data &&
        (active ? (
          <ActiveList list={active} />
        ) : (
          <p className={styles.warning} role="status">
            {t("gateway.ru.noList")}
          </p>
        ))}

      {update.isError && <Notice>{update.error.message}</Notice>}
      {updateJob.data && updateDone && (
        <p className={styles.result} data-ok={updateJob.data.status === "success"} role="status">
          {updateJob.data.status === "success"
            ? ingestMessage((updateJob.data.result ?? { status: "active" }) as { status: string })
            : t("gateway.ru.updateFailed", { error: updateJob.data.error ?? "" })}
        </p>
      )}
      {uploaded && (
        <p className={styles.result} data-ok="true" role="status">
          {uploaded}
        </p>
      )}
      {apply.isError && <Notice>{apply.error.message}</Notice>}
      {lastApply && (
        <p className={styles.hint}>
          {lastApply.status === "success" ? (
            <>
              {t("gateway.ru.applied", {
                routes: Number((lastApply.result as { routes?: number } | null)?.routes ?? 0),
              })}{" "}
              <time dateTime={lastApply.created_at} title={exactTime(lastApply.created_at)}>
                {timeAgo(lastApply.created_at)}
              </time>
            </>
          ) : (
            t("gateway.ru.applyFailed", { error: lastApply.error ?? "" })
          )}
        </p>
      )}

      {(geoip.data?.candidates ?? []).map((candidate) => (
        <Candidate key={candidate.id} list={candidate} manage={manage} />
      ))}

      <Overrides
        manage={manage}
        loading={overrides.isPending}
        list={overrides.data ?? []}
        onAdd={() => setOverrideOpen(true)}
      />
      {overrides.isError && <Notice>{overrides.error.message}</Notice>}

      {manage && (
        <>
          <UploadListDialog open={uploadOpen} onOpenChange={setUploadOpen} onDone={setUploaded} />
          <AddOverrideDialog open={overrideOpen} onOpenChange={setOverrideOpen} />
        </>
      )}
    </div>
  );
}

function ActiveList({ list }: { list: GeoipList }) {
  const { t } = useTranslation();
  return (
    <dl className={styles.facts}>
      <dt>{t("gateway.ru.active.version")}</dt>
      <dd>{list.version}</dd>
      <dt>{t("gateway.ru.active.prefixes")}</dt>
      <dd>{t("gateway.ru.active.prefixesValue", { prefixes: list.prefixes_count })}</dd>
      <dt>{t("gateway.ru.active.activated")}</dt>
      <dd>
        {list.activated_at ? (
          <time dateTime={list.activated_at} title={exactTime(list.activated_at)}>
            {timeAgo(list.activated_at)}
          </time>
        ) : (
          "—"
        )}
      </dd>
    </dl>
  );
}

/** Новый список сильно отличается от активного: на шлюзы он не идёт, пока админ не подтвердит. */
function Candidate({ list, manage }: { list: GeoipList; manage: boolean }) {
  const { t } = useTranslation();
  const activate = useActivateList();
  return (
    <div className={styles.warning} role="group" aria-label={t("gateway.ru.candidate.title")}>
      <p>
        <strong>{t("gateway.ru.candidate.title")}</strong>{" "}
        {t("gateway.ru.candidate.text", { version: list.version, prefixes: list.prefixes_count })}
      </p>
      {manage && (
        <div className={styles.row}>
          <Button
            busy={activate.isPending}
            busyLabel={t("gateway.ru.candidate.busy")}
            onClick={() => activate.mutate(list.id)}
          >
            {t("gateway.ru.candidate.activate")}
          </Button>
        </div>
      )}
      {activate.isError && <Notice>{activate.error.message}</Notice>}
    </div>
  );
}

function Overrides({
  manage,
  loading,
  list,
  onAdd,
}: {
  manage: boolean;
  loading: boolean;
  list: { id: string; network: string; action: "direct" | "tunnel"; note: string | null }[];
  onAdd: () => void;
}) {
  const { t } = useTranslation();
  const remove = useRemoveOverride();
  return (
    <div className={styles.block}>
      <div className={styles.blockHead}>
        <h4 className={styles.blockTitle}>{t("gateway.ru.overrides.title")}</h4>
        {manage && (
          <Button variant="quiet" onClick={onAdd}>
            {t("gateway.ru.overrides.add.trigger")}
          </Button>
        )}
      </div>
      {list.length === 0 ? (
        <p className={styles.hint}>{loading ? t("gateway.ru.loading") : t("gateway.ru.overrides.empty")}</p>
      ) : (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col">{t("gateway.ru.overrides.columns.network")}</th>
                <th scope="col">{t("gateway.ru.overrides.columns.action")}</th>
                <th scope="col">{t("gateway.ru.overrides.columns.note")}</th>
                {manage && (
                  <th scope="col">
                    <span className="visually-hidden">{t("gateway.exits.columns.actions")}</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {list.map((item) => (
                <tr key={item.id} data-tone={item.action === "direct" ? "ok" : "process"}>
                  <th scope="row" className={styles.name}>
                    <code className={styles.keyValue}>{item.network}</code>
                  </th>
                  <td data-label={t("gateway.ru.overrides.columns.action")}>
                    {t(`gateway.ru.overrides.actions.${item.action}`)}
                  </td>
                  <td data-label={t("gateway.ru.overrides.columns.note")}>{item.note ?? "—"}</td>
                  {manage && (
                    <td data-actions>
                      <ConfirmDialog
                        trigger={
                          <Button
                            variant="quiet"
                            aria-label={t("gateway.ru.overrides.remove.trigger", { network: item.network })}
                          >
                            {t("gateway.ru.overrides.remove.button")}
                          </Button>
                        }
                        title={t("gateway.ru.overrides.remove.title", { network: item.network })}
                        consequences={t(`gateway.ru.overrides.remove.consequences.${item.action}`)}
                        actionLabel={t("gateway.ru.overrides.remove.action")}
                        busyLabel={t("gateway.ru.overrides.remove.busy")}
                        onConfirm={() => remove.mutateAsync(item.id)}
                      />
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
