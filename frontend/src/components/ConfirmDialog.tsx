import * as AlertDialog from "@radix-ui/react-alert-dialog";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { Button } from "./Button";
import styles from "./Dialog.module.css";
import { Notice } from "./Notice";
import { TextField } from "./TextField";

/**
 * Бросается из onConfirm, когда действие нужно подтвердить ещё раз, уже зная подробности (например, «останется 3
 * клиента без выхода»): диалог остаётся открытым, показывает текст, а кнопка меняется на `actionLabel`.
 * Следующий onConfirm получает `again = true`.
 */
export class NeedsConfirmation extends Error {
  constructor(
    message: string,
    readonly actionLabel: string,
  ) {
    super(message);
  }
}

interface ConfirmDialogProps {
  trigger: ReactNode;
  title: string;
  /** Что именно случится — конкретно, без «вы уверены?». */
  consequences: ReactNode;
  /** Текст кнопки — само действие («Снять протокол»), а не «OK». */
  actionLabel: string;
  busyLabel: string;
  /** Если задано — действие доступно только после ввода этой строки (необратимые действия). */
  confirmText?: string;
  /** again — подтверждение повторное, после NeedsConfirmation. */
  onConfirm: (again: boolean) => Promise<unknown>;
}

export function ConfirmDialog({
  trigger,
  title,
  consequences,
  actionLabel,
  busyLabel,
  confirmText,
  onConfirm,
}: ConfirmDialogProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [again, setAgain] = useState<NeedsConfirmation | null>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await onConfirm(again !== null);
      setOpen(false);
    } catch (exc) {
      if (exc instanceof NeedsConfirmation) {
        setAgain(exc);
      } else {
        setError(exc instanceof ApiError ? exc.message : t("common.errors.actionFailed"));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <AlertDialog.Root
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        setTyped("");
        setError(null);
        setAgain(null);
      }}
    >
      <AlertDialog.Trigger asChild>{trigger}</AlertDialog.Trigger>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className={styles.overlay} />
        <AlertDialog.Content className={styles.content} data-danger="true">
          <AlertDialog.Title className={styles.title}>{title}</AlertDialog.Title>
          <AlertDialog.Description className={styles.description}>{consequences}</AlertDialog.Description>
          {confirmText && (
            <TextField
              label={t("common.confirm.typeToConfirm", { text: confirmText })}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoComplete="off"
            />
          )}
          {again && <Notice>{again.message}</Notice>}
          {error && <Notice>{error}</Notice>}
          <div className={styles.actions}>
            <AlertDialog.Cancel asChild>
              <Button variant="secondary">{t("common.cancel")}</Button>
            </AlertDialog.Cancel>
            <Button
              variant="danger"
              busy={busy}
              busyLabel={busyLabel}
              disabled={confirmText !== undefined && typed !== confirmText}
              onClick={() => void confirm()}
            >
              {again ? again.actionLabel : actionLabel}
            </Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
