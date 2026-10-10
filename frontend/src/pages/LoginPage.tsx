import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useLocation } from "react-router";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { BrandMark } from "../components/BrandMark";
import { Button } from "../components/Button";
import { LanguageSwitch } from "../components/LanguageSwitch";
import { Notice } from "../components/Notice";
import { TextField } from "../components/TextField";
import styles from "./LoginPage.module.css";

export function LoginPage() {
  const { t } = useTranslation();
  const { state, login } = useAuth();
  const location = useLocation();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (state.status === "signed-in") {
    const from = (location.state as { from?: string } | null)?.from ?? "/servers";
    return <Navigate to={from} replace />;
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      await login(String(form.get("email")), String(form.get("password")));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : t("auth.login.failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.page}>
      <form className={styles.form} onSubmit={submit} aria-labelledby="login-title">
        <BrandMark />
        <h1 id="login-title" className={styles.title}>
          {t("auth.login.title")}
        </h1>
        {error && <Notice>{error}</Notice>}
        <TextField
          label={t("auth.login.email")}
          name="email"
          type="email"
          autoComplete="username"
          required
          autoFocus
        />
        <TextField
          label={t("auth.login.password")}
          name="password"
          type="password"
          autoComplete="current-password"
          required
        />
        <Button type="submit" busy={busy} busyLabel={t("auth.login.busy")}>
          {t("auth.login.submit")}
        </Button>
        <LanguageSwitch />
      </form>
    </div>
  );
}
