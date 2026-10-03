import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, unwrap, type Grant, type Job, type Server, type VPNCredential } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Notice } from "../../components/Notice";
import { formatDay, isLive } from "../../lib/access";
import { isUnfinished } from "../../lib/jobs";
import { ConfigDialog } from "./ConfigDialog";
import styles from "./UserPage.module.css";
import { liveCredential, userKeys } from "./useUser";

type RowState = { tone: "ok" | "process" | "down" | "idle"; text: string };

/** Что сейчас с доступом к одному серверу — одной фразой для человека. */
export function grantState(grant: Grant, credential: VPNCredential | null, job: Job | undefined): RowState {
  if (!isLive(grant)) {
    if (credential) return { tone: "process", text: "Убираем клиента с сервера…" };
    return { tone: "idle", text: grant.status === "expired" ? "Срок истёк" : "Отозван" };
  }
  if (job && isUnfinished(job)) {
    return { tone: "process", text: job.type === "revoke_access" ? "Отзываем конфиг…" : "Выдаём конфиг…" };
  }
  if (credential) return { tone: "ok", text: `Конфиг выдан ${formatDay(credential.created_at)}` };
  if (job?.status === "failed" && job.type === "create_user")
    return { tone: "down", text: "Конфиг не выдан" };
  return { tone: "idle", text: "Конфиг не выдан" };
}

export function GrantRow({
  grant,
  server,
  credentials,
  job,
  userName,
  onJob,
}: {
  grant: Grant;
  server: Server | undefined;
  credentials: VPNCredential[] | undefined;
  job: Job | undefined;
  userName: string;
  onJob: (grantId: string, job: Job) => void;
}) {
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const [configOpen, setConfigOpen] = useState(false);
  const credential = liveCredential(credentials);
  const state = grantState(grant, credential, job);
  const live = isLive(grant);
  const busy = job !== undefined && isUnfinished(job);
  const manage = can("access:manage");

  const issue = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/access/{access_grant_id}/credentials", {
          params: { path: { access_grant_id: grant.id } },
        }),
        {
          409: "Конфиг уже выдаётся или период доступа ещё не начался.",
        },
      ),
    onSuccess: (created) => onJob(grant.id, created),
  });

  const serverName = server?.name ?? "Сервер удалён";
  const fileName = `${userName}-${serverName}`.toLowerCase().replace(/[^a-z0-9а-яё_-]+/gi, "-");

  return (
    <li className={styles.grant} data-tone={state.tone}>
      <div className={styles.grantHead}>
        <span className={styles.grantServer}>
          {serverName}
          {server && <span className={styles.address}>{server.host}</span>}
        </span>
        <span className={styles.grantState} role="status">
          {state.text}
        </span>
      </div>
      {job?.status === "failed" && job.error && !credential && (
        <p className={styles.grantError}>{job.error}</p>
      )}
      {issue.isError && <Notice>{issue.error.message}</Notice>}

      {live && !busy && (
        <div className={styles.row}>
          {credential && can("config:reveal") && (
            <Button onClick={() => setConfigOpen(true)}>Показать конфиг</Button>
          )}
          {!credential && manage && (
            <Button busy={issue.isPending} busyLabel="Ставим в очередь…" onClick={() => issue.mutate()}>
              {job?.status === "failed" ? "Повторить выдачу" : "Выдать конфиг"}
            </Button>
          )}
          {credential && manage && (
            <ConfirmDialog
              trigger={<Button variant="quiet">Отозвать конфиг</Button>}
              title="Отозвать конфиг?"
              consequences={`Клиент будет удалён с сервера ${serverName}: подключиться с этим конфигом больше не получится. Доступ к серверу останется — можно выдать новый конфиг.`}
              actionLabel="Отозвать конфиг"
              busyLabel="Отзываем…"
              onConfirm={async () => {
                const result = await unwrap(
                  api.POST("/api/v1/credentials/{credential_id}/revoke", {
                    params: { path: { credential_id: credential.id } },
                  }),
                );
                if ("type" in result) onJob(grant.id, result);
                else void queryClient.invalidateQueries({ queryKey: userKeys.credentials(grant.id) });
              }}
            />
          )}
          {manage && (
            <ConfirmDialog
              trigger={<Button variant="quiet">Убрать сервер</Button>}
              title={`Убрать доступ к серверу ${serverName}?`}
              consequences="Конфиг для этого сервера перестанет работать, клиент будет удалён с сервера. Доступ к остальным серверам и срок не изменятся."
              actionLabel="Убрать сервер"
              busyLabel="Убираем…"
              onConfirm={async () => {
                await unwrap(
                  api.POST("/api/v1/access/{access_grant_id}/revoke", {
                    params: { path: { access_grant_id: grant.id } },
                  }),
                );
                await queryClient.invalidateQueries({ queryKey: userKeys.grants(grant.subscription_id) });
                await queryClient.invalidateQueries({ queryKey: userKeys.credentials(grant.id) });
              }}
            />
          )}
        </div>
      )}
      {credential && (
        <ConfigDialog
          credentialId={credential.id}
          fileName={fileName}
          open={configOpen}
          onOpenChange={setConfigOpen}
        />
      )}
    </li>
  );
}
