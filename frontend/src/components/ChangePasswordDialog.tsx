import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "./Button";
import { ChangePasswordForm } from "./ChangePasswordForm";
import { Dialog, DialogActions } from "./Dialog";

/** «Сменить пароль» в блоке учётки. После смены остальные устройства придётся войти заново — об этом сказано. */
export function ChangePasswordDialog() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(false);

  function change(next: boolean) {
    setOpen(next);
    if (!next) setDone(false);
  }

  return (
    <>
      <Button variant="quiet" onClick={() => change(true)}>
        {t("auth.changePassword.open")}
      </Button>
      <Dialog
        open={open}
        onOpenChange={change}
        title={t("auth.changePassword.dialogTitle")}
        description={done ? undefined : t("auth.changePassword.dialogDescription")}
      >
        {done ? (
          <>
            <p role="status">{t("auth.changePassword.done")}</p>
            <DialogActions>
              <Button onClick={() => change(false)}>{t("auth.changePassword.close")}</Button>
            </DialogActions>
          </>
        ) : (
          <ChangePasswordForm
            currentLabel={t("auth.changePassword.currentPassword")}
            onDone={() => setDone(true)}
            secondary={
              <Button variant="quiet" onClick={() => change(false)}>
                {t("common.cancel")}
              </Button>
            }
          />
        )}
      </Dialog>
    </>
  );
}
