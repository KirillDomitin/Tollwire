import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../../components/Button";
import { Dialog, DialogActions } from "../../../components/Dialog";
import { Notice } from "../../../components/Notice";
import { TextField } from "../../../components/TextField";
import { NOTE_MAX, looksLikeCidr } from "./gateway";
import styles from "./Gateway.module.css";
import { useAddOverride } from "./useGatewayActions";

/**
 * Исключение из списка «Россия напрямую»: сеть, которая вопреки списку идёт напрямую (с адреса шлюза) или через выход.
 * Формат проверяется до запроса, остальное (публичность, размер, адреса серверов) — бэкендом; его отказ показывается
 * понятным текстом.
 */
export function AddOverrideDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const [action, setAction] = useState<"direct" | "tunnel">("direct");
  const [problem, setProblem] = useState<string | null>(null);
  const add = useAddOverride();

  function close(next: boolean) {
    if (!next) {
      add.reset();
      setProblem(null);
      setAction("direct");
    }
    onOpenChange(next);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const network = String(form.get("network")).trim();
    const note = String(form.get("note")).trim();
    if (!looksLikeCidr(network)) {
      setProblem(t("gateway.ru.errors.format"));
      return;
    }
    setProblem(null);
    add.mutate({ network, action, note: note || null }, { onSuccess: () => close(false) });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={t("gateway.ru.overrides.add.title")}
      description={t("gateway.ru.overrides.add.description")}
    >
      <form className={styles.form} onSubmit={submit} noValidate>
        <TextField
          label={t("gateway.ru.overrides.add.network")}
          name="network"
          placeholder="5.45.200.0/24"
          autoComplete="off"
          inputMode="decimal"
          error={problem}
        />
        <fieldset className={styles.choice}>
          <legend className="visually-hidden">{t("gateway.ru.overrides.add.action")}</legend>
          {(["direct", "tunnel"] as const).map((value) => (
            <label key={value}>
              <input
                type="radio"
                name="action"
                checked={action === value}
                onChange={() => setAction(value)}
              />
              {t(`gateway.ru.overrides.actions.${value}`)}
            </label>
          ))}
        </fieldset>
        <p className={styles.hint}>{t(`gateway.ru.overrides.add.hint.${action}`)}</p>
        <TextField
          label={t("gateway.ru.overrides.add.note")}
          name="note"
          maxLength={NOTE_MAX}
          autoComplete="off"
        />
        {add.isError && <Notice>{add.error.message}</Notice>}
        <DialogActions>
          <Button variant="quiet" onClick={() => close(false)}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" busy={add.isPending} busyLabel={t("gateway.ru.overrides.add.busy")}>
            {t("gateway.ru.overrides.add.submit")}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
