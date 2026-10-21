import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { api, unwrap } from "../../api/client";
import { Button } from "../../components/Button";
import { CountrySelect } from "../../components/CountrySelect";
import { Dialog, DialogActions } from "../../components/Dialog";
import { PlusIcon } from "../../components/icons";
import { Notice } from "../../components/Notice";
import { TextField } from "../../components/TextField";
import { NAME_MAX_WITH_FLAG, withFlag } from "../../lib/flags";
import styles from "./ServerPage.module.css";

/** Новый сервер: только адрес и имя. Дальше — карточка сервера с шагами подготовки. */
export function AddServerDialog() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);

  const create = useMutation({
    mutationFn: (body: { name: string; host: string; ssh_port: number; description: string | null }) =>
      unwrap(api.POST("/api/v1/servers", { body }), {
        409: t("servers.add.errors.conflict"),
        422: t("servers.add.errors.invalid"),
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
      <Button icon={<PlusIcon size={18} />} onClick={() => setOpen(true)}>
        {t("servers.add.trigger")}
      </Button>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next) create.reset();
          setOpen(next);
        }}
        title={t("servers.add.title")}
        description={t("servers.add.description")}
      >
        <form className={styles.form} onSubmit={submit}>
          <CountrySelect name="flag" />
          <TextField
            label={t("servers.add.name")}
            name="name"
            required
            maxLength={NAME_MAX_WITH_FLAG}
            placeholder={t("servers.add.namePlaceholder")}
          />
          <TextField
            label={t("servers.add.host")}
            name="host"
            required
            maxLength={100}
            pattern="[A-Za-z0-9.:\-]+"
            title={t("servers.add.hostTitle")}
            spellCheck={false}
            autoComplete="off"
          />
          <TextField
            label={t("servers.add.port")}
            name="ssh_port"
            type="number"
            required
            min={1}
            max={65535}
            defaultValue={22}
          />
          <TextField label={t("servers.add.note")} name="description" maxLength={300} />
          {create.isError && <Notice>{create.error.message}</Notice>}
          <DialogActions>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" busy={create.isPending} busyLabel={t("servers.add.busy")}>
              {t("servers.add.submit")}
            </Button>
          </DialogActions>
        </form>
      </Dialog>
    </>
  );
}
