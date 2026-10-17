import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../components/Button";
import { exactTime, timeAgo } from "../../lib/time";
import { importableCount, newClientsCount, type AppProtocol } from "./discovery";
import { ImportClientsDialog } from "./ImportClientsDialog";
import styles from "./ServerPage.module.css";

/**
 * Найденная на сервере установка приложения AmneziaVPN. Панель её не ставила и не меняет: можно только подхватить
 * её клиентов (перенос в базу панели, право servers:manage). Отчёт — результат последнего поиска.
 */
export function AmneziaAppSection({
  serverId,
  protocols,
  checkedAt,
  manage,
  importing,
}: {
  serverId: string;
  protocols: AppProtocol[];
  checkedAt: string | null;
  manage: boolean;
  /** Перенос уже идёт (задача в очереди или выполняется). */
  importing: boolean;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const importable = protocols.reduce((sum, protocol) => sum + importableCount(protocol), 0);
  const stopped = protocols.some((protocol) => protocol.state !== "running");

  return (
    <section className={styles.section} aria-labelledby="amnezia-app-title">
      <h2 id="amnezia-app-title" className={styles.sectionTitle}>
        {t("servers.discovery.title")}
      </h2>
      <p className={styles.hint}>{t("servers.discovery.description")}</p>

      {protocols.map((protocol) => (
        <div role="group" key={protocol.type} aria-label={t(`servers.discovery.protocols.${protocol.type}`)}>
          <dl className={styles.facts}>
            <dt>{t("servers.discovery.container")}</dt>
            <dd>
              <code className={styles.keyValue}>{protocol.container}</code>{" "}
              <span className={styles.hint}>{t(`servers.discovery.protocols.${protocol.type}`)}</span>
            </dd>

            <dt>{t("servers.discovery.port")}</dt>
            <dd>
              {protocol.port
                ? t("servers.discovery.portValue", { port: protocol.port })
                : t("servers.discovery.portUnknown")}
            </dd>

            <dt>{t("servers.discovery.state")}</dt>
            <dd>
              {protocol.state === "running" || protocol.state === "stopped"
                ? t(`servers.discovery.states.${protocol.state}`)
                : protocol.state}
            </dd>

            <dt>{t("servers.discovery.clients")}</dt>
            <dd>
              {t("servers.discovery.clientsValue", {
                total: protocol.clients.length,
                fresh: newClientsCount(protocol),
              })}
            </dd>
          </dl>
        </div>
      ))}

      {stopped && <p className={styles.hint}>{t("servers.discovery.stoppedHint")}</p>}
      {checkedAt && (
        <p className={styles.hint}>
          {t("servers.discovery.checked")}{" "}
          <time dateTime={checkedAt} title={exactTime(checkedAt)}>
            {timeAgo(checkedAt)}
          </time>
        </p>
      )}

      {manage &&
        (importable > 0 ? (
          <div className={styles.row}>
            <Button
              busy={importing}
              busyLabel={t("servers.discovery.importing")}
              onClick={() => setOpen(true)}
            >
              {t("servers.discovery.importButton")}
            </Button>
          </div>
        ) : (
          <p className={styles.hint}>{t("servers.discovery.nothingNew")}</p>
        ))}
      {manage && (
        <ImportClientsDialog serverId={serverId} protocols={protocols} open={open} onOpenChange={setOpen} />
      )}
    </section>
  );
}
