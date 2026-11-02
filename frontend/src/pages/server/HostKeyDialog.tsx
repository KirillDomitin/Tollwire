import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import { api, unwrap, type HostKeyScan } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import styles from "./ServerPage.module.css";
import { keys } from "./useServer";

/**
 * Закрепление ключа хоста. Панель получает ключ, который сервер предъявляет сейчас (без входа — пароль не уходит),
 * а админ сверяет отпечаток с тем, что показывает сам сервер в консоли хостинга. Только после «совпадает» ключ
 * закрепляется: дальше панель подключается к серверу, только если он докажет владение именно этим ключом.
 */
export function HostKeyDialog({
  serverId,
  open,
  onOpenChange,
}: {
  serverId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const path = { params: { path: { server_id: serverId } } };
  const [scan, setScan] = useState<HostKeyScan | null>(null);

  const scanKey = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/v1/servers/{server_id}/host-key/scan", path), {
        502: t("servers.hostKey.errors.noAnswer"),
      }),
    onSuccess: setScan,
  });
  const pin = useMutation({
    mutationFn: (publicKey: string) =>
      unwrap(api.PUT("/api/v1/servers/{server_id}/host-key", { ...path, body: { public_key: publicKey } })),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.server(serverId), exact: true });
      void queryClient.invalidateQueries({ queryKey: ["servers"] });
      close(false);
    },
  });

  function close(next: boolean) {
    if (!next) {
      setScan(null);
      scanKey.reset();
      pin.reset();
    }
    onOpenChange(next);
  }

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={t("servers.hostKey.title")}
      description={t("servers.hostKey.description")}
    >
      {!scan && (
        <>
          {scanKey.isError && <Notice>{scanKey.error.message}</Notice>}
          <DialogActions>
            <Button variant="secondary" onClick={() => close(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              busy={scanKey.isPending}
              busyLabel={t("servers.hostKey.getting")}
              onClick={() => scanKey.mutate()}
            >
              {t("servers.hostKey.get")}
            </Button>
          </DialogActions>
        </>
      )}

      {scan && (
        <>
          <div className={styles.keyBlock}>
            <p className={styles.keyLabel}>{t("servers.hostKey.fingerprint")}</p>
            <code className={styles.keyValue}>{scan.fingerprint}</code>
          </div>
          {scan.matches_pinned === false && (
            <Notice>{t("servers.hostKey.mismatch", { fingerprint: scan.pinned_fingerprint })}</Notice>
          )}
          <p className={styles.hint}>
            <Trans i18nKey="servers.hostKey.verify" components={{ code: <code /> }} />
          </p>
          {pin.isError && <Notice>{pin.error.message}</Notice>}
          <DialogActions>
            <Button variant="secondary" onClick={() => close(false)}>
              {t("servers.hostKey.notMatching")}
            </Button>
            <Button
              busy={pin.isPending}
              busyLabel={t("servers.hostKey.pinning")}
              onClick={() => pin.mutate(scan.public_key)}
            >
              {t("servers.hostKey.matching")}
            </Button>
          </DialogActions>
        </>
      )}
    </Dialog>
  );
}
