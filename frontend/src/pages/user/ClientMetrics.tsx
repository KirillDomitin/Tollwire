import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, unwrap } from "../../api/client";
import { timeAgo, exactTime } from "../../lib/time";
import { formatBytes } from "../../lib/units";
import styles from "./UserPage.module.css";

export function ClientMetrics({
  serverId,
  protocolId,
  credentialId,
}: {
  serverId: string;
  protocolId: string;
  credentialId: string;
}) {
  const { t } = useTranslation();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);
  const metrics = useQuery({
    queryKey: ["client-metrics", serverId],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/servers/{server_id}/clients/monitoring", {
          params: { path: { server_id: serverId } },
        }),
      ),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const protocol = metrics.data?.protocols.find((item) => item.protocol_id === protocolId);
  const client = protocol?.clients?.find((item) => item.credential_id === credentialId);
  const old =
    protocol?.stale ||
    metrics.isError ||
    (protocol?.sampled_at && now - new Date(protocol.sampled_at).getTime() >= 300_000);
  const available = !!protocol?.sampled_at && !!client;

  return (
    <section className={styles.clientMetrics} aria-label={t("users.metrics.title")}>
      {metrics.isPending ? (
        <p className={styles.hint}>{t("users.metrics.loading")}</p>
      ) : !available ? (
        <p className={styles.hint}>{t("users.metrics.unavailable")}</p>
      ) : (
        <>
          {(old || metrics.isRefetchError) && (
            <p role="status" className={styles.hint}>
              {t("users.metrics.stale")}
            </p>
          )}
          <dl className={styles.clientMetricValues}>
            <div>
              <dt>{t("users.metrics.handshake")}</dt>
              <dd>
                {client.latest_handshake ? (
                  <time dateTime={client.latest_handshake} title={exactTime(client.latest_handshake)}>
                    {timeAgo(client.latest_handshake, new Date(now))}
                  </time>
                ) : old ? (
                  "—"
                ) : (
                  t("users.metrics.never")
                )}
              </dd>
            </div>
            <div>
              <dt>{t("users.metrics.downloaded")}</dt>
              <dd>{formatBytes(client.downloaded_bytes)}</dd>
            </div>
            <div>
              <dt>{t("users.metrics.uploaded")}</dt>
              <dd>{formatBytes(client.uploaded_bytes)}</dd>
            </div>
          </dl>
          <p className={styles.hint}>{t("users.metrics.period")}</p>
        </>
      )}
      {protocol?.sampled_at && (
        <p className={styles.hint}>
          {t("users.metrics.updated")}{" "}
          <time dateTime={protocol.sampled_at} title={exactTime(protocol.sampled_at)}>
            {timeAgo(protocol.sampled_at, new Date(now))}
          </time>
        </p>
      )}
    </section>
  );
}
