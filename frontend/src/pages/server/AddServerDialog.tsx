import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";

import { api, unwrap } from "../../api/client";
import { Button } from "../../components/Button";
import { CountrySelect } from "../../components/CountrySelect";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { TextField } from "../../components/TextField";
import { NAME_MAX_WITH_FLAG, withFlag } from "../../lib/flags";
import styles from "./ServerPage.module.css";

/** Новый сервер: только адрес и имя. Дальше — карточка сервера с шагами подготовки. */
export function AddServerDialog() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);

  const create = useMutation({
    mutationFn: (body: { name: string; host: string; ssh_port: number; description: string | null }) =>
      unwrap(api.POST("/api/v1/servers", { body }), {
        409: "Сервер с таким именем уже есть. Выберите другое имя.",
        422: "Проверьте адрес: только латиница, цифры, точки, дефисы и двоеточия, без пробелов и «ssh://».",
      }),
    onSuccess: (server) => {
      void queryClient.invalidateQueries({ queryKey: ["servers"] });
      setOpen(false);
      void navigate(`/servers/${server.id}`);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const description = String(form.get("description")).trim();
    create.mutate({
      name: withFlag(String(form.get("flag")), String(form.get("name")).trim()),
      host: String(form.get("host")).trim(),
      ssh_port: Number(form.get("ssh_port")),
      description: description || null,
    });
  }

  return (
    <>
      <Button onClick={() => setOpen(true)}>Добавить сервер</Button>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next) create.reset();
          setOpen(next);
        }}
        title="Новый сервер"
        description="Панель пока ничего не делает с сервером — только запоминает адрес. Подготовка — по шагам на его карточке."
      >
        <form className={styles.form} onSubmit={submit}>
          <CountrySelect name="flag" />
          <TextField
            label="Имя в панели"
            name="name"
            required
            maxLength={NAME_MAX_WITH_FLAG}
            placeholder="Например, frankfurt-1"
          />
          <TextField
            label="Адрес (IP или домен)"
            name="host"
            required
            maxLength={100}
            pattern="[A-Za-z0-9.:\-]+"
            title="IP-адрес или домен, без пробелов и протокола"
            spellCheck={false}
            autoComplete="off"
          />
          <TextField
            label="SSH-порт"
            name="ssh_port"
            type="number"
            required
            min={1}
            max={65535}
            defaultValue={22}
          />
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
