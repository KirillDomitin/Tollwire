import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "./Button";
import { ChangePasswordForm } from "./ChangePasswordForm";
import { Dialog, DialogActions } from "./Dialog";

/** Сам диалог смены пароля: монтируется при открытии (см. ChangePasswordDialog), закрытие размонтирует его. */
export function ChangePasswordDialogContent({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const [done, setDone] = useState(false);

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={t("auth.changePassword.dialogTitle")}
      description={done ? undefined : t("auth.changePassword.dialogDescription")}
    >
      {done ? (
        <>
          <p role="status">{t("auth.changePassword.done")}</p>
          <DialogActions>
            <Button onClick={onClose}>{t("auth.changePassword.close")}</Button>
          </DialogActions>
        </>
      ) : (
        <ChangePasswordForm
          currentLabel={t("auth.changePassword.currentPassword")}
          onDone={() => setDone(true)}
          secondary={
            <Button variant="secondary" onClick={onClose}>
              {t("common.cancel")}
            </Button>
          }
        />
      )}
    </Dialog>
  );
}
