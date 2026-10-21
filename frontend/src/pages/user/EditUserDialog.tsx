import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api, unwrap, type VPNUser } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { TextField } from "../../components/TextField";
import styles from "./UserPage.module.css";
import { userKeys } from "./useUser";

/**
 * Данные пользователя: имя, email, заметка. Конфиги и доступы от них не зависят, на серверах ничего не меняется.
 * У подхваченного из AmneziaVPN клиента email может не быть: тогда он необязателен, а пустое поле не отправляется.
 */
export function EditUserDialog({ user }: { user: VPNUser }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const hadEmail = user.email !== null;

  const save = useMutation({
    mutationFn: (body: { username: string; email?: string; description: string | null }) =>
      unwrap(api.PATCH("/api/v1/vpn-users/{user_id}", { params: { path: { user_id: user.id } }, body }), {
        409: t("users.edit.errors.conflict"),
        422: t("users.edit.errors.invalid"),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: userKeys.user(user.id), exact: true });
      void queryClient.invalidateQueries({ queryKey: ["vpn-users"] });
      close(false);
    },
  });

  function close(next: boolean) {
    if (!next) save.reset();
    setOpen(next);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const description = String(form.get("description")).trim();
    const email = String(form.get("email")).trim();
    save.mutate({
      username: String(form.get("username")).trim(),
      ...(email ? { email } : {}),
      description: description || null,
    });
  }

  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)}>
        {t("users.edit.trigger")}
      </Button>
      <Dialog
        open={open}
        onOpenChange={close}
        title={t("users.edit.title")}
        description={t("users.edit.description")}
      >
        <form className={styles.form} onSubmit={submit}>
          <TextField
            label={t("users.add.name")}
            name="username"
            defaultValue={user.username}
            required
            maxLength={100}
            autoComplete="off"
          />
          <TextField
            label={hadEmail ? t("users.add.email") : t("users.edit.emailOptional")}
            name="email"
            type="email"
            defaultValue={user.email ?? ""}
            required={hadEmail}
            maxLength={100}
            autoComplete="off"
          />
          <TextField
            label={t("users.add.note")}
            name="description"
            defaultValue={user.description ?? ""}
            maxLength={300}
          />
          {save.isError && <Notice>{save.error.message}</Notice>}
          <DialogActions>
            <Button variant="secondary" onClick={() => close(false)}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" busy={save.isPending} busyLabel={t("users.edit.busy")}>
              {t("users.edit.submit")}
            </Button>
          </DialogActions>
        </form>
      </Dialog>
    </>
  );
}
