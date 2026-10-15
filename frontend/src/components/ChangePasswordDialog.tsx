import { lazy, Suspense, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "./Button";

// Диалог тянет Radix Dialog и форму — грузим при первом открытии, в основном бандле остаётся только кнопка.
const ChangePasswordDialogContent = lazy(() =>
  import("./ChangePasswordDialogContent").then((module) => ({ default: module.ChangePasswordDialogContent })),
);

/** «Сменить пароль» в блоке учётки. После смены остальные устройства придётся войти заново — об этом сказано. */
export function ChangePasswordDialog() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  return (
    <>
      <Button variant="quiet" onClick={() => setOpen(true)}>
        {t("auth.changePassword.open")}
      </Button>
      {open && (
        <Suspense fallback={null}>
          <ChangePasswordDialogContent onClose={() => setOpen(false)} />
        </Suspense>
      )}
    </>
  );
}
