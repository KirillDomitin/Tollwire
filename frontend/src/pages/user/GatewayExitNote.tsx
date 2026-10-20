import { useTranslation } from "react-i18next";

import { useAssignments, useExits } from "../server/gateway/useGateway";
import styles from "./UserPage.module.css";

/**
 * Через какой выход идёт клиент шлюза: слот и имя сервера-выхода. Адресов и ключей здесь нет — только то, что отдаёт
 * `GET .../gateway/assignments`. Пока данные не пришли (или нет права на чтение серверов), строка не показывается.
 */
export function GatewayExitNote({ serverId, credentialId }: { serverId: string; credentialId: string }) {
  const { t } = useTranslation();
  const exits = useExits(serverId);
  const assignments = useAssignments(serverId);
  if (!exits.data || !assignments.data) return null;

  const assignment = assignments.data.find((item) => item.credential_id === credentialId);
  const exit = assignment ? exits.data.find((item) => item.id === assignment.exit_id) : undefined;
  return assignment ? (
    <p className={styles.hint}>
      {t("users.grant.exit", { slot: assignment.slot, name: exit?.exit_server_name ?? "—" })}
    </p>
  ) : (
    <p className={styles.grantError}>{t("users.grant.exitNone")}</p>
  );
}
