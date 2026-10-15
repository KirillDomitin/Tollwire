import { useState, type FormEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { MIN_PASSWORD_LENGTH, useAuth } from "../auth/AuthContext";
import i18n from "../i18n";
import { Button } from "./Button";
import styles from "./ChangePasswordForm.module.css";
import { Notice } from "./Notice";
import { TextField } from "./TextField";

/** Что не так с новым паролем, ещё до обращения к серверу. null — можно отправлять. */
function problemWith(current: string, next: string, repeat: string): string | null {
  if (next.length < MIN_PASSWORD_LENGTH) {
    return i18n.t("auth.changePassword.problems.tooShort", { min: MIN_PASSWORD_LENGTH });
  }
  if (next !== repeat) return i18n.t("auth.changePassword.problems.mismatch");
  if (next === current) return i18n.t("auth.changePassword.problems.same");
  return null;
}

interface Props {
  currentLabel: string;
  /** Пароль сменён. */
  onDone?: () => void;
  /** Вторая кнопка рядом с «Сохранить пароль»: «Отмена» в диалоге, «Выйти» на экране временного пароля. */
  secondary?: ReactNode;
}

export function ChangePasswordForm({ currentLabel, onDone, secondary }: Props) {
  const { t } = useTranslation();
  const { changePassword } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const current = String(form.get("current"));
    const next = String(form.get("next"));
    const problem = problemWith(current, next, String(form.get("repeat")));
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await changePassword(current, next);
      onDone?.();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : t("auth.changePassword.failed"));
    } finally {
      setBusy(false);
    }
  }

  // noValidate: сообщения свои, на языке интерфейса, а не браузерные на языке системы.
  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      {error && <Notice>{error}</Notice>}
      <TextField
        label={currentLabel}
        name="current"
        type="password"
        autoComplete="current-password"
        required
        autoFocus
      />
      <TextField
        label={t("auth.changePassword.newPassword")}
        name="next"
        type="password"
        autoComplete="new-password"
        required
      />
      <TextField
        label={t("auth.changePassword.repeatPassword")}
        name="repeat"
        type="password"
        autoComplete="new-password"
        required
      />
      <div className={styles.actions}>
        {secondary}
        <Button type="submit" busy={busy} busyLabel={t("auth.changePassword.busy")}>
          {t("auth.changePassword.submit")}
        </Button>
      </div>
    </form>
  );
}
