import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, unwrap, type Grant, type Job, type Server, type VPNCredential } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Notice } from "../../components/Notice";
import { StatusPill } from "../../components/StatusPill";
import i18n from "../../i18n";
import { formatDay, isLive } from "../../lib/access";
import { isUnfinished } from "../../lib/jobs";
import { keys } from "../server/useServer";
import { GatewayExitNote } from "./GatewayExitNote";
import styles from "./UserPage.module.css";
import { liveCredential, userKeys } from "./useUser";

// Диалог конфига тянет библиотеку QR-кодов — грузим при первом открытии, а не вместе со страницей.
const ConfigDialog = lazy(() =>
  import("./ConfigDialog").then((module) => ({ default: module.ConfigDialog })),
);

const TONES = { ok: "online", process: "process", down: "offline", idle: "neutral" } as const;

type RowState = { tone: "ok" | "process" | "down" | "idle"; text: string };

/** Что сейчас с доступом к одному серверу — одной фразой для человека. */
export function grantState(
  grant: Grant,
  credential: VPNCredential | null,
  job: Job | undefined,
  /** Клиент этого доступа подхвачен из AmneziaVPN (сейчас или раньше): панель сама клиента не выдаёт. */
  fromApp = false,
): RowState {
  if (!isLive(grant)) {
    if (credential) return { tone: "process", text: i18n.t("users.grant.removingClient") };
    return {
      tone: "idle",
      text: grant.status === "expired" ? i18n.t("users.grant.expired") : i18n.t("users.grant.revoked"),
    };
  }
  if (job && isUnfinished(job)) {
    return {
      tone: "process",
      text:
        job.type === "revoke_access"
          ? i18n.t("users.grant.revokingConfig")
          : i18n.t("users.grant.issuingConfig"),
    };
  }
  if (credential) {
    return {
      tone: "ok",
      text:
        credential.source === "imported"
          ? i18n.t("users.grant.configImported", { date: formatDay(credential.created_at) })
          : i18n.t("users.grant.configIssued", { date: formatDay(credential.created_at) }),
    };
  }
  if (fromApp) return { tone: "idle", text: i18n.t("users.grant.importedRevoked") };
  if (job?.status === "failed" && job.type === "create_user")
    return { tone: "down", text: i18n.t("users.grant.configNotIssued") };
  return { tone: "idle", text: i18n.t("users.grant.configNotIssued") };
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
  const { t } = useTranslation();
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const [configOpen, setConfigOpen] = useState(false);
  const credential = liveCredential(credentials);
  // История credential'ов: подхваченный клиент остаётся признаком доступа к установке приложения и после отзыва.
  const fromApp = credentials?.some((item) => item.source === "imported") ?? false;
  const imported = credential?.source === "imported";
  const state = grantState(grant, credential, job, fromApp);
  const live = isLive(grant);
  const busy = job !== undefined && isUnfinished(job);
  const manage = can("access:manage");
  // Доступ к шлюзу выглядит как обычный (конфиг, QR, ссылка), но у него есть выход: узнаём тип протокола доступа.
  const protocols = useQuery({
    queryKey: keys.protocols(grant.server_id),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/servers/{server_id}/protocols", {
          params: { path: { server_id: grant.server_id } },
        }),
      ),
    enabled: server !== undefined && can("servers:read"),
  });
  const gateway = protocols.data?.some(
    (item) => item.id === grant.protocol_id && item.type === "amnezia_gateway",
  );

  const issue = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/access/{access_grant_id}/credentials", {
          params: { path: { access_grant_id: grant.id } },
        }),
        {
          409: t("users.grant.errors.issueConflict"),
        },
      ),
    onSuccess: (created) => onJob(grant.id, created),
  });

  const serverName = server?.name ?? t("users.grant.serverDeleted");
  const fileName = `${userName}-${serverName}`.toLowerCase().replace(/[^a-z0-9а-яё_-]+/gi, "-");

  return (
    <li className={styles.grant} data-tone={state.tone}>
      <div className={styles.grantHead}>
        <span className={styles.grantServer}>
          {serverName}
          {server && <span className={styles.address}>{server.host}</span>}
          {gateway && <span className={styles.kind}>{t("users.grant.gateway")}</span>}
        </span>
        <span role="status">
          <StatusPill tone={TONES[state.tone]}>{state.text}</StatusPill>
        </span>
      </div>
      {job?.status === "failed" && job.error && !credential && (
        <p className={styles.grantError}>{job.error}</p>
      )}
      {issue.isError && <Notice>{issue.error.message}</Notice>}
      {gateway && live && !busy && credential && !imported && (
        <GatewayExitNote serverId={grant.server_id} credentialId={credential.id} />
      )}
      {live && !busy && fromApp && (
        <p className={styles.hint}>
          {imported ? t("users.grant.importedNote") : t("users.grant.importedRevokedNote")}
        </p>
      )}

      {live && !busy && (
        <div className={styles.row}>
          {credential && !imported && can("config:reveal") && (
            <Button size="sm" onClick={() => setConfigOpen(true)}>
              {t("users.grant.showConfig")}
            </Button>
          )}
          {!credential && !fromApp && manage && (
            <Button
              size="sm"
              busy={issue.isPending}
              busyLabel={t("users.grant.queueBusy")}
              onClick={() => issue.mutate()}
            >
              {job?.status === "failed" ? t("users.grant.retry") : t("users.grant.issue")}
            </Button>
          )}
          {credential && manage && (
            <ConfirmDialog
              trigger={
                <Button size="sm" variant="secondary">
                  {t("users.grant.revokeConfig.trigger")}
                </Button>
              }
              title={t("users.grant.revokeConfig.title")}
              consequences={
                imported
                  ? t("users.grant.revokeConfig.consequencesImported", { server: serverName })
                  : t("users.grant.revokeConfig.consequences", { server: serverName })
              }
              actionLabel={t("users.grant.revokeConfig.action")}
              busyLabel={t("users.grant.revokeConfig.busy")}
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
              trigger={
                <Button size="sm" variant="secondary">
                  {t("users.grant.removeServer.trigger")}
                </Button>
              }
              title={t("users.grant.removeServer.title", { server: serverName })}
              consequences={t("users.grant.removeServer.consequences")}
              actionLabel={t("users.grant.removeServer.action")}
              busyLabel={t("users.grant.removeServer.busy")}
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
      {credential && !imported && configOpen && (
        <Suspense fallback={null}>
          <ConfigDialog
            credentialId={credential.id}
            fileName={fileName}
            open={configOpen}
            onOpenChange={setConfigOpen}
          />
        </Suspense>
      )}
    </li>
  );
}
