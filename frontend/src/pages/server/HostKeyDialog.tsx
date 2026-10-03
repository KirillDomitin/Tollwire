import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

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
  const queryClient = useQueryClient();
  const path = { params: { path: { server_id: serverId } } };
  const [scan, setScan] = useState<HostKeyScan | null>(null);

  const scanKey = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/v1/servers/{server_id}/host-key/scan", path), {
        502: "Сервер не ответил на SSH-порту. Проверьте адрес, порт и что сервер включён.",
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
      title="Закрепить ключ сервера"
      description="Панель запомнит ключ сервера и дальше будет подключаться только к нему. Так пароль не уйдёт подменённому серверу."
    >
      {!scan && (
        <>
          {scanKey.isError && <Notice>{scanKey.error.message}</Notice>}
          <DialogActions>
            <Button variant="quiet" onClick={() => close(false)}>
              Отмена
            </Button>
            <Button busy={scanKey.isPending} busyLabel="Получаем ключ…" onClick={() => scanKey.mutate()}>
              Получить ключ
            </Button>
          </DialogActions>
        </>
      )}

      {scan && (
        <>
          <div className={styles.keyBlock}>
            <p className={styles.keyLabel}>Отпечаток ключа, который предъявил сервер</p>
            <code className={styles.keyValue}>{scan.fingerprint}</code>
          </div>
          {scan.matches_pinned === false && (
            <Notice>
              Ключ отличается от закреплённого ({scan.pinned_fingerprint}). Закрепляйте новый, только если
              знаете, что сервер переустанавливали.
            </Notice>
          )}
          <p className={styles.hint}>
            Сверьте его с консолью хостинга. На сервере выполните{" "}
            <code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code> — отпечаток должен совпасть до
            символа.
          </p>
          {pin.isError && <Notice>{pin.error.message}</Notice>}
          <DialogActions>
            <Button variant="quiet" onClick={() => close(false)}>
              Не совпадает — отменить
            </Button>
            <Button busy={pin.isPending} busyLabel="Закрепляем…" onClick={() => pin.mutate(scan.public_key)}>
              Совпадает — закрепить
            </Button>
          </DialogActions>
        </>
      )}
    </Dialog>
  );
}
