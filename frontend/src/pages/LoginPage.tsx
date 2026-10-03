import { useState, type FormEvent } from "react";
import { Navigate, useLocation } from "react-router";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { BrandMark } from "../components/BrandMark";
import { Button } from "../components/Button";
import { Notice } from "../components/Notice";
import { TextField } from "../components/TextField";
import styles from "./LoginPage.module.css";

export function LoginPage() {
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
      setError(exc instanceof ApiError ? exc.message : "Не удалось войти. Повторите попытку.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.page}>
      <form className={styles.form} onSubmit={submit} aria-labelledby="login-title">
        <BrandMark />
        <h1 id="login-title" className={styles.title}>
          Вход в панель
        </h1>
        {error && <Notice>{error}</Notice>}
        <TextField label="Email" name="email" type="email" autoComplete="username" required autoFocus />
        <TextField label="Пароль" name="password" type="password" autoComplete="current-password" required />
        <Button type="submit" busy={busy} busyLabel="Входим…">
          Войти
        </Button>
      </form>
    </div>
  );
}
