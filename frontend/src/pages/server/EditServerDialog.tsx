import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api, unwrap, type Schemas, type Server } from "../../api/client";
import { Button } from "../../components/Button";
import { CountrySelect } from "../../components/CountrySelect";
import { Dialog, DialogActions } from "../../components/Dialog";
import { PencilIcon } from "../../components/icons";
import { Notice } from "../../components/Notice";
import { TextField } from "../../components/TextField";
import { NAME_MAX_WITH_FLAG, splitFlag, withFlag } from "../../lib/flags";
import { CitySelect } from "./CitySelect";
import styles from "./ServerPage.module.css";
import { keys } from "./useServer";

/** Имя, город и заметка сервера. Другой адрес машины требует нового ключа и доступа. */
export function EditServerDialog({ server }: { server: Server }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [cityDraft, setCityDraft] = useState<string>();
  const current = splitFlag(server.name);
  const savedCity = server.location_city_id ?? "";

  const save = useMutation({
    mutationFn: (body: Schemas["ServerUpdate"]) =>
      unwrap(api.PATCH("/api/v1/servers/{server_id}", { params: { path: { server_id: server.id } }, body }), {
        409: t("servers.add.errors.conflict"),
        422: t("servers.edit.errors.invalid"),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.server(server.id), exact: true });
      void queryClient.invalidateQueries({ queryKey: ["servers"] });
      close(false);
    },
  });

  function close(next: boolean) {
    if (!next) {
      save.reset();
      setCityDraft(undefined);
    }
    setOpen(next);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const description = String(form.get("description")).trim();
    save.mutate({
      name: withFlag(String(form.get("flag")), String(form.get("name")).trim()),
      description: description || null,
      ...(cityDraft !== undefined && cityDraft !== savedCity ? { location_city_id: cityDraft || null } : {}),
    });
  }

  return (
    <>
      <Button icon={<PencilIcon size={16} />} onClick={() => setOpen(true)}>
        {t("servers.edit.trigger")}
      </Button>
      <Dialog
        open={open}
        onOpenChange={close}
        title={t("servers.edit.title")}
        description={t("servers.edit.description")}
      >
        <form className={styles.form} onSubmit={submit}>
          <CountrySelect name="flag" defaultValue={current.flag} />
          <CitySelect value={cityDraft ?? savedCity} onChange={setCityDraft} />
          <TextField
            label={t("servers.add.name")}
            name="name"
            defaultValue={current.rest}
            required
            maxLength={NAME_MAX_WITH_FLAG}
          />
          <TextField
            label={t("servers.add.note")}
            name="description"
            defaultValue={server.description ?? ""}
            maxLength={300}
          />
          {save.isError && <Notice>{save.error.message}</Notice>}
          <DialogActions>
            <Button variant="secondary" onClick={() => close(false)}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" busy={save.isPending} busyLabel={t("servers.edit.busy")}>
              {t("servers.edit.submit")}
            </Button>
          </DialogActions>
        </form>
      </Dialog>
    </>
  );
}
