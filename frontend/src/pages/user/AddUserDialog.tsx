import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";

import { api, unwrap } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { TextField } from "../../components/TextField";
import styles from "./UserPage.module.css";

/** Новый VPN-пользователь — это только запись о человеке. Доступ и конфиг выдаются на его странице. */
export function AddUserDialog() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);

  const create = useMutation({
    mutationFn: (body: { username: string; email: string; description: string | null }) =>
      unwrap(api.POST("/api/v1/vpn-users", { body }), {
        409: "Пользователь с таким email уже есть.",
        422: "Проверьте email — он должен быть полным, например name@example.com.",
      }),
    onSuccess: (user) => {
      void queryClient.invalidateQueries({ queryKey: ["vpn-users"] });
      setOpen(false);
      void navigate(`/users/${user.id}`);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const description = String(form.get("description")).trim();
    create.mutate({
      username: String(form.get("username")).trim(),
      email: String(form.get("email")).trim(),
      description: description || null,
    });
  }

  return (
    <>
      <Button onClick={() => setOpen(true)}>Добавить пользователя</Button>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next) create.reset();
          setOpen(next);
        }}
        title="Новый пользователь"
        description="Доступ к серверам выдаётся после — на странице пользователя."
      >
        <form className={styles.form} onSubmit={submit}>
          <TextField label="Имя" name="username" required maxLength={100} autoComplete="off" />
          <TextField label="Email" name="email" type="email" required maxLength={100} autoComplete="off" />
          <TextField label="Заметка (необязательно)" name="description" maxLength={300} />
          {create.isError && <Notice>{create.error.message}</Notice>}
          <DialogActions>
            <Button variant="quiet" onClick={() => setOpen(false)}>
              Отмена
            </Button>
            <Button type="submit" busy={create.isPending} busyLabel="Добавляем…">
              Добавить
            </Button>
          </DialogActions>
        </form>
      </Dialog>
    </>
  );
}
