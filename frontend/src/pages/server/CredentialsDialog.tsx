import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { api, unwrap } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { TextField } from "../../components/TextField";
import styles from "./ServerPage.module.css";
import { keys } from "./useServer";

type AuthType = "password" | "ssh_key";

/** SSH-учётка сервера. Секрет уходит на бэкенд один раз (там шифруется) и обратно не показывается никогда. */
export function CredentialsDialog({
  serverId,
  open,
  onOpenChange,
}: {
  serverId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [authType, setAuthType] = useState<AuthType>("password");

  const save = useMutation({
    mutationFn: (body: { auth_type: AuthType; username: string; secret: string }) =>
      unwrap(
        api.PUT("/api/v1/servers/{server_id}/credentials", {
          params: { path: { server_id: serverId } },
          body,
        }),
        {
          422: "Проверьте данные: пользователь — латиница без пробелов, ключ — целиком, без пароля (passphrase).",
        },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.credential(serverId) });
      onOpenChange(false);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    save.mutate({
      auth_type: authType,
      username: String(form.get("username")).trim(),
      secret: String(form.get("secret")),
    });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) save.reset();
        onOpenChange(next);
      }}
      title="SSH-доступ к серверу"
      description="Панель хранит секрет зашифрованным и использует его только для подключения к этому серверу."
    >
      <form className={styles.form} onSubmit={submit}>
        <fieldset className={styles.choice}>
          <legend className={styles.keyLabel}>Вход</legend>
          {(["password", "ssh_key"] as const).map((value) => (
            <label key={value}>
              <input
                type="radio"
                name="auth_type"
                checked={authType === value}
                onChange={() => setAuthType(value)}
              />{" "}
              {value === "password" ? "по паролю" : "по SSH-ключу"}
            </label>
          ))}
        </fieldset>
        <TextField label="Пользователь" name="username" defaultValue="root" required autoComplete="off" />
        {authType === "password" ? (
          <TextField label="Пароль" name="secret" type="password" required autoComplete="new-password" />
        ) : (
          <label className={styles.textarea}>
            <span className={styles.keyLabel}>Приватный ключ (OpenSSH или PEM, без пароля)</span>
            <textarea name="secret" required rows={6} spellCheck={false} autoComplete="off" />
          </label>
        )}
        {save.isError && <Notice>{save.error.message}</Notice>}
        <DialogActions>
          <Button variant="quiet" onClick={() => onOpenChange(false)}>
            Отмена
          </Button>
          <Button type="submit" busy={save.isPending} busyLabel="Сохраняем…">
            Сохранить доступ
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
