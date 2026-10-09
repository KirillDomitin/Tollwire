import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { api, unwrap, type Server } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { TextField } from "../../components/TextField";
import styles from "./ServerPage.module.css";
import { keys } from "./useServer";

/** Имя и заметка сервера. Адрес здесь не меняется: другой адрес — другая машина, ему нужны новый ключ и доступ. */
export function EditServerDialog({ server }: { server: Server }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const save = useMutation({
    mutationFn: (body: { name: string; description: string | null }) =>
      unwrap(
        api.PATCH("/api/v1/servers/{server_id}", { params: { path: { server_id: server.id } }, body }),
        {
          409: "Сервер с таким именем уже есть. Выберите другое имя.",
          422: "Проверьте имя: от 1 до 100 символов.",
        },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.server(server.id), exact: true });
      void queryClient.invalidateQueries({ queryKey: ["servers"] });
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
    save.mutate({ name: String(form.get("name")).trim(), description: description || null });
  }

  return (
    <>
      <Button variant="quiet" onClick={() => setOpen(true)}>
        Изменить
      </Button>
      <Dialog
        open={open}
        onOpenChange={close}
        title="Изменить сервер"
        description="Имя и заметка видны только в панели, на самом сервере ничего не меняется."
      >
        <form className={styles.form} onSubmit={submit}>
          <TextField label="Имя в панели" name="name" defaultValue={server.name} required maxLength={100} />
          <TextField
            label="Заметка (необязательно)"
            name="description"
            defaultValue={server.description ?? ""}
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
