import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";

import { api, unwrap, type Job } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { TextField } from "../../components/TextField";
import styles from "./ServerPage.module.css";
import { keys } from "./useServer";

type AuthType = "password" | "ssh_key";

type Submitted =
  | { kind: "credential"; body: { auth_type: AuthType; username: string; secret: string } }
  | { kind: "panel-key"; body: { username: string; password: string } };

/**
 * SSH-учётка сервера. Секрет уходит на бэкенд один раз (там шифруется) и обратно не показывается никогда.
 * С паролем по умолчанию панель сразу ставит свой ключ: пароль нужен один раз, дальше вход по ключу панели.
 */
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
  const [panelKey, setPanelKey] = useState(true);
  const panelKeyHint = useId();
  const path = { params: { path: { server_id: serverId } } };

  const save = useMutation({
    mutationFn: async (submitted: Submitted): Promise<Job | null> => {
      if (submitted.kind === "panel-key") {
        return unwrap(api.POST("/api/v1/servers/{server_id}/ssh-key", { ...path, body: submitted.body }), {
          409: "Ключ не поставить: сначала закрепите ключ сервера, или установка ключа уже идёт.",
          422: "Проверьте данные: пользователь — латиница без пробелов.",
        });
      }
      await unwrap(api.PUT("/api/v1/servers/{server_id}/credentials", { ...path, body: submitted.body }), {
        422: "Проверьте данные: пользователь — латиница без пробелов, ключ — целиком, без пароля (passphrase).",
      });
      return null;
    },
    onSuccess: (job) => {
      // Задача установки ключа сразу появляется в журнале и опрашивается, пока не завершится.
      if (job) queryClient.setQueryData<Job[]>(keys.jobs(serverId), (jobs = []) => [job, ...jobs]);
      void queryClient.invalidateQueries({ queryKey: keys.credential(serverId) });
      onOpenChange(false);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const username = String(form.get("username")).trim();
    const secret = String(form.get("secret"));
    save.mutate(
      authType === "password" && panelKey
        ? { kind: "panel-key", body: { username, password: secret } }
        : { kind: "credential", body: { auth_type: authType, username, secret } },
    );
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
          <>
            <TextField label="Пароль" name="secret" type="password" required autoComplete="new-password" />
            <div className={styles.check}>
              <label>
                <input
                  type="checkbox"
                  checked={panelKey}
                  onChange={(event) => setPanelKey(event.target.checked)}
                  aria-describedby={panelKeyHint}
                />{" "}
                Заменить пароль ключом панели
              </label>
              <p id={panelKeyHint} className={styles.hint}>
                Панель войдёт по паролю один раз, поставит на сервер свой ключ и дальше будет входить по нему.
                Пароль в панели не останется.
              </p>
            </div>
          </>
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
