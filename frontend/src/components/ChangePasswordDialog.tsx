import { lazy, Suspense } from "react";

// Диалог тянет Radix Dialog и форму — грузим при первом открытии, в основном бандле остаётся только вызывающий его пункт.
const ChangePasswordDialogContent = lazy(() =>
  import("./ChangePasswordDialogContent").then((module) => ({ default: module.ChangePasswordDialogContent })),
);

/** Диалог «Сменить пароль» из меню пользователя. После смены остальные устройства придётся войти заново — об этом сказано. */
export function ChangePasswordDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return (
    <Suspense fallback={null}>
      <ChangePasswordDialogContent onClose={onClose} />
    </Suspense>
  );
}
