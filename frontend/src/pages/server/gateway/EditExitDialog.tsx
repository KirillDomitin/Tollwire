import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../../components/Button";
import { Dialog, DialogActions } from "../../../components/Dialog";
import { Notice } from "../../../components/Notice";
import { TextField } from "../../../components/TextField";
import { MAX_CLIENTS_LIMIT, WEIGHT_MAX, WEIGHT_MIN, parseWhole, type GatewayExit } from "./gateway";
import styles from "./Gateway.module.css";
import { useUpdateExit } from "./useGatewayActions";

/** Вес и предел клиентов выхода. Меняется только в панели: туннель и клиенты на нём не затрагиваются. */
export function EditExitDialog({ serverId, exit }: { serverId: string; exit: GatewayExit }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [problems, setProblems] = useState<{ weight?: string; limit?: string }>({});
  const update = useUpdateExit(serverId, exit.id);

  function close(next: boolean) {
    if (!next) {
      update.reset();
      setProblems({});
    }
    setOpen(next);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const weight = parseWhole(String(form.get("weight")), WEIGHT_MIN, WEIGHT_MAX);
    const limitText = String(form.get("limit")).trim();
    const limit = limitText === "" ? null : parseWhole(limitText, 1, MAX_CLIENTS_LIMIT);
    const found: typeof problems = {};
    if (weight === null)
      found.weight = t("gateway.exits.add.problems.weight", { min: WEIGHT_MIN, max: WEIGHT_MAX });
    if (limitText !== "" && limit === null)
      found.limit = t("gateway.exits.add.problems.limit", { max: MAX_CLIENTS_LIMIT });
    setProblems(found);
    if (weight === null || Object.keys(found).length > 0) return;
    update.mutate({ weight, max_clients: limit }, { onSuccess: () => close(false) });
  }

  return (
    <>
      <Button
        variant="secondary"
        onClick={() => setOpen(true)}
        aria-label={t("gateway.exits.edit.trigger", { name: exit.exit_server_name })}
      >
        {t("gateway.exits.edit.button")}
      </Button>
      <Dialog
        open={open}
        onOpenChange={close}
        title={t("gateway.exits.edit.title", { name: exit.exit_server_name })}
        description={t("gateway.exits.edit.description")}
      >
        <form className={styles.form} onSubmit={submit} noValidate>
          <TextField
            label={t("gateway.exits.add.weight", { min: WEIGHT_MIN, max: WEIGHT_MAX })}
            name="weight"
            inputMode="numeric"
            defaultValue={String(exit.weight)}
            autoComplete="off"
            error={problems.weight}
          />
          <TextField
            label={t("gateway.exits.add.limit")}
            name="limit"
            inputMode="numeric"
            defaultValue={exit.max_clients === null ? "" : String(exit.max_clients)}
            placeholder={t("gateway.exits.add.limitPlaceholder")}
            autoComplete="off"
            error={problems.limit}
          />
          {update.isError && <Notice>{update.error.message}</Notice>}
          <DialogActions>
            <Button variant="secondary" onClick={() => close(false)}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" busy={update.isPending} busyLabel={t("servers.edit.busy")}>
              {t("servers.edit.submit")}
            </Button>
          </DialogActions>
        </form>
      </Dialog>
    </>
  );
}
