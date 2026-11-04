import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, unwrap, type Job, type Schemas } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { StatusPill } from "../../components/StatusPill";
import { isUnfinished } from "../../lib/jobs";
import { exactTime } from "../../lib/time";
import styles from "./ServerPage.module.css";
import { keys, useStartJob } from "./useServer";

type Observation = Schemas["ProtocolUpdateObservation"];
type OwnProtocol = "amnezia_wg" | "amnezia_gateway";

export function useProtocolUpdates(serverId: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.protocolUpdates(serverId),
    queryFn: () => unwrap(api.GET("/api/v1/servers/{server_id}/protocol/updates", {
      params: { path: { server_id: serverId } },
    })),
    enabled,
    // This endpoint reads the cache; it never polls the VPN server or registry.
    refetchInterval: enabled ? 60_000 : false,
  });
}

export function ProtocolUpdateStatus({ serverId, kind, observation, jobs }: {
  serverId: string;
  kind: OwnProtocol;
  observation?: Observation;
  jobs: Job[];
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const [open, setOpen] = useState(false);
  const start = useStartJob(serverId);
  const updating = jobs.some((job) => job.type === "update_protocol" && isUnfinished(job));
  const status = observation?.status ?? "unknown";
  const stale = observation?.stale ?? false;
  const tone = stale || status === "unknown" || status === "unsupported"
    ? "neutral" : status === "available" ? "warning" : "online";

  async function confirm() {
    await start.mutateAsync(kind === "amnezia_gateway" ? "gateway-update" : "protocol-update");
    setOpen(false);
  }

  return (
    <div className={styles.protocolUpdate}>
      <StatusPill tone={tone}>{t(`servers.protocolUpdates.status.${status}`)}</StatusPill>
      {stale && <span className={styles.hint}>{t("servers.protocolUpdates.stale")}</span>}
      {observation?.checked_at && (
        <time className={styles.hint} dateTime={observation.checked_at}>
          {t("servers.protocolUpdates.checked", { when: exactTime(observation.checked_at) })}
        </time>
      )}
      {can("servers:manage") && (
        <Button variant="secondary" size="sm" disabled={updating} onClick={() => { start.reset(); setOpen(true); }}>
          {updating ? t("servers.protocolUpdates.updating") : t("servers.protocolUpdates.update")}
        </Button>
      )}
      <Dialog open={open} onOpenChange={setOpen}
        title={t("servers.protocolUpdates.title", { protocol: kind === "amnezia_gateway" ? t("servers.maintenance.gateway") : t("servers.maintenance.amnezia") })}
        description={t("servers.protocolUpdates.consequences")}>
        {start.isError && <Notice>{start.error.message}</Notice>}
        <DialogActions>
          <Button variant="secondary" disabled={start.isPending} onClick={() => setOpen(false)}>{t("common.cancel")}</Button>
          <Button busy={start.isPending} busyLabel={t("servers.card.queueBusy")} onClick={() => void confirm().catch(() => undefined)}>
            {t("servers.protocolUpdates.confirm")}
          </Button>
        </DialogActions>
      </Dialog>
    </div>
  );
}
