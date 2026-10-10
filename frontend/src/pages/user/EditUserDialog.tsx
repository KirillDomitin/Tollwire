import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { api, unwrap, type VPNUser } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { TextField } from "../../components/TextField";
import styles from "./UserPage.module.css";
import { userKeys } from "./useUser";

/** Данные пользователя: имя, email, заметка. Конфиги и доступы от них не зависят, на серверах ничего не меняется. */
export function EditUserDialog({ user }: { user: VPNUser }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const save = useMutation({
    mutationFn: (body: { username: string; email: string; description: string | null }) =>
      unwrap(api.PATCH("/api/v1/vpn-users/{user_id}", { params: { path: { user_id: user.id } }, body }), {
        409: "Пользователь с таким email уже есть.",
        422: "Проверьте данные: email должен быть полным, например name@example.com, а имя — не пустым.",
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
    save.mutate({
      username: String(form.get("username")).trim(),
      email: String(form.get("email")).trim(),
      description: description || null,
    });
  }

  return (
    <>
      <Button variant="quiet" onClick={() => setOpen(true)}>
        Изменить
      </Button>
      <Dialog
        open={open}
        onOpenChange={close}
        title="Изменить пользователя"
        description="Имя, email и заметка видны только в панели. Выданные конфиги и доступ не меняются."
      >
        <form className={styles.form} onSubmit={submit}>
          <TextField
            label="Имя"
            name="username"
            defaultValue={user.username}
            required
            maxLength={100}
            autoComplete="off"
          />
          <TextField
            label="Email"
            name="email"
            type="email"
            defaultValue={user.email}
            required
            maxLength={100}
            autoComplete="off"
          />
          <TextField
            label="Заметка (необязательно)"
            name="description"
            defaultValue={user.description ?? ""}
            maxLength={300}
          />
          {save.isError && <Notice>{save.error.message}</Notice>}
          <DialogActions>
            <Button variant="quiet" onClick={() => close(false)}>
              Отмена
            </Button>
            <Button type="submit" busy={save.isPending} busyLabel="Сохраняем…">
              Сохранить
            </Button>
          </DialogActions>
        </form>
      </Dialog>
    </>
  );
}
