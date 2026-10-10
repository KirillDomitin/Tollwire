import { useMutation, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, unwrap, type Grant, type Job, type Server, type VPNCredential } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Notice } from "../../components/Notice";
import i18n from "../../i18n";
import { formatDay, isLive } from "../../lib/access";
import { isUnfinished } from "../../lib/jobs";
import styles from "./UserPage.module.css";
import { liveCredential, userKeys } from "./useUser";

// Диалог конфига тянет библиотеку QR-кодов — грузим при первом открытии, а не вместе со страницей.
const ConfigDialog = lazy(() =>
  import("./ConfigDialog").then((module) => ({ default: module.ConfigDialog })),
);

type RowState = { tone: "ok" | "process" | "down" | "idle"; text: string };

/** Что сейчас с доступом к одному серверу — одной фразой для человека. */
export function grantState(grant: Grant, credential: VPNCredential | null, job: Job | undefined): RowState {
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
      text: i18n.t("users.grant.configIssued", { date: formatDay(credential.created_at) }),
    };
  }
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
            <Button onClick={() => setConfigOpen(true)}>{t("users.grant.showConfig")}</Button>
          )}
          {!credential && manage && (
            <Button
              busy={issue.isPending}
              busyLabel={t("users.grant.queueBusy")}
              onClick={() => issue.mutate()}
            >
              {job?.status === "failed" ? t("users.grant.retry") : t("users.grant.issue")}
            </Button>
          )}
          {credential && manage && (
            <ConfirmDialog
              trigger={<Button variant="quiet">{t("users.grant.revokeConfig.trigger")}</Button>}
              title={t("users.grant.revokeConfig.title")}
              consequences={t("users.grant.revokeConfig.consequences", { server: serverName })}
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
              trigger={<Button variant="quiet">{t("users.grant.removeServer.trigger")}</Button>}
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
      {credential && configOpen && (
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
