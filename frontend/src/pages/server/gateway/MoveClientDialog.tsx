import { useId, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../../components/Button";
import { Dialog, DialogActions } from "../../../components/Dialog";
import { Notice } from "../../../components/Notice";
import type { GatewayAssignment, GatewayExit } from "./gateway";
import styles from "./Gateway.module.css";
import { useMoveClient } from "./useGatewayActions";

/**
 * Перенести клиента на другой выход (и закрепить за ним) или назначить выход клиенту, у которого его нет.
 * Предлагаются только действующие выходы: бэкенд не примет перенос на выход, который не активен или отказал.
 */
export function MoveClientDialog({
  serverId,
  credentialId,
  clientName,
  assignment,
  exits,
}: {
  serverId: string;
  credentialId: string;
  clientName: string;
  /** Текущее назначение; null — клиент без выхода. */
  assignment: GatewayAssignment | null;
  exits: GatewayExit[];
}) {
  const { t } = useTranslation();
  const selectId = useId();
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState("");
  const [pin, setPin] = useState(false);
  const move = useMoveClient(serverId, credentialId);

  const targets = exits.filter((exit) => exit.state === "active" && exit.id !== assignment?.exit_id);
  const selected = targets.some((exit) => exit.id === chosen) ? chosen : (targets[0]?.id ?? "");

  function close(next: boolean) {
    if (!next) {
      move.reset();
      setChosen("");
      setPin(false);
    }
    setOpen(next);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    move.mutate({ exit_id: selected, pin }, { onSuccess: () => close(false) });
  }

  return (
    <>
      <Button
        variant="secondary"
        onClick={() => setOpen(true)}
        aria-label={t(
          assignment ? "gateway.assignments.move.trigger" : "gateway.assignments.move.assignTrigger",
          {
            name: clientName,
          },
        )}
      >
        {assignment ? t("gateway.assignments.move.button") : t("gateway.assignments.move.assign")}
      </Button>
      <Dialog
        open={open}
        onOpenChange={close}
        title={t(assignment ? "gateway.assignments.move.title" : "gateway.assignments.move.assignTitle", {
          name: clientName,
        })}
        description={t(
          assignment ? "gateway.assignments.move.description" : "gateway.assignments.move.assignDescription",
        )}
      >
        <form className={styles.form} onSubmit={submit}>
          <div className={styles.field}>
            <label htmlFor={selectId} className={styles.fieldLabel}>
              {t("gateway.assignments.move.exit")}
            </label>
            <select
              id={selectId}
              className={styles.select}
              value={selected}
              disabled={targets.length === 0}
              onChange={(event) => setChosen(event.target.value)}
            >
              {targets.map((exit) => (
                <option key={exit.id} value={exit.id}>
                  {t("gateway.assignments.exitOption", { slot: exit.slot, name: exit.exit_server_name })}
                </option>
              ))}
            </select>
            {targets.length === 0 && <p className={styles.hint}>{t("gateway.assignments.move.noTargets")}</p>}
          </div>
          <div className={styles.check}>
            <label>
              <input type="checkbox" checked={pin} onChange={(event) => setPin(event.target.checked)} />
              {t("gateway.assignments.move.pin")}
            </label>
            <p className={styles.hint}>{t("gateway.assignments.move.pinHint")}</p>
          </div>
          {move.isError && <Notice>{move.error.message}</Notice>}
          <DialogActions>
            <Button variant="secondary" onClick={() => close(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              type="submit"
              disabled={!selected}
              busy={move.isPending}
              busyLabel={t("gateway.assignments.move.busy")}
            >
              {assignment ? t("gateway.assignments.move.submit") : t("gateway.assignments.move.assignSubmit")}
            </Button>
          </DialogActions>
        </form>
      </Dialog>
    </>
  );
}
