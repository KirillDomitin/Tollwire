import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useLocation } from "react-router";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { ShieldMark } from "../components/BrandMark";
import { Button } from "../components/Button";
import { EyeIcon, EyeOffIcon, LockIcon, UserIcon } from "../components/icons";
import { LanguageSwitch } from "../components/LanguageSwitch";
import { Notice } from "../components/Notice";
import { TextField } from "../components/TextField";
import styles from "./LoginPage.module.css";

// Только `npm run dev`: в прод-сборке import.meta.env.DEV === false, ветки вырезаются.
const DEV_LOGIN_ALIAS = import.meta.env.DEV ? { admin: "admin@admin.com" } : {};

function resolveLogin(value: string): string {
  return (DEV_LOGIN_ALIAS as Record<string, string>)[value.trim()] ?? value;
}

export function LoginPage() {
  const { t } = useTranslation();
  const { state, login } = useAuth();
  const location = useLocation();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

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
      await login(resolveLogin(String(form.get("email"))), String(form.get("password")));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : t("auth.login.failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.page}>
      <Landscape />
      <div className={styles.language}>
        <LanguageSwitch />
      </div>
      <div className={styles.column}>
        <div className={styles.brand}>
          <ShieldMark size={64} />
          <div>
            <p className={styles.name}>WirePilot</p>
            <p className={styles.tagline}>{t("auth.login.tagline")}</p>
          </div>
        </div>
        <form className={styles.card} onSubmit={submit} aria-labelledby="login-title">
          <h1 id="login-title" className={styles.title}>
            {t("auth.login.title")}
          </h1>
          <p className={styles.lead}>{t("auth.login.lead")}</p>
          {error && <Notice>{error}</Notice>}
          <TextField
            label={t("auth.login.email")}
            hideLabel
            placeholder={t("auth.login.email")}
            icon={<UserIcon />}
            name="email"
            type={import.meta.env.DEV ? "text" : "email"}
            autoComplete="username"
            required
            autoFocus
          />
          <TextField
            label={t("auth.login.password")}
            hideLabel
            placeholder={t("auth.login.password")}
            icon={<LockIcon />}
            trailing={
              <button
                type="button"
                className={styles.reveal}
                aria-label={showPassword ? t("auth.login.hidePassword") : t("auth.login.showPassword")}
                aria-pressed={showPassword}
                onClick={() => setShowPassword((value) => !value)}
              >
                {showPassword ? <EyeOffIcon /> : <EyeIcon />}
              </button>
            }
            name="password"
            type={showPassword ? "text" : "password"}
            autoComplete="current-password"
            required
          />
          <Button type="submit" busy={busy} busyLabel={t("auth.login.busy")}>
            {t("auth.login.submit")}
          </Button>
          <p className={styles.secure}>{t("auth.login.secure")}</p>
        </form>
      </div>
    </div>
  );
}

/** Фон входа: тёмный градиент и силуэты гор, нарисованные SVG (внешних картинок нет — CSP их не пустит). */
function Landscape() {
  return (
    <svg
      className={styles.landscape}
      viewBox="0 0 1440 600"
      preserveAspectRatio="xMidYMax slice"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id="login-far" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#2b4373" stopOpacity="0.55" />
          <stop offset="1" stopColor="#101c36" stopOpacity="0" />
        </linearGradient>
        <linearGradient id="login-mid" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#1c2f58" />
          <stop offset="1" stopColor="#0b1428" />
        </linearGradient>
        <linearGradient id="login-near" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#101d3a" />
          <stop offset="1" stopColor="#070c19" />
        </linearGradient>
      </defs>
      <path
        d="M0 330 140 250 230 300 360 190 470 280 560 230 700 340 820 240 930 300 1060 180 1180 290 1290 230 1440 320V600H0Z"
        fill="url(#login-far)"
      />
      <path
        d="M0 420 120 360 210 400 340 300 420 350 520 270 640 390 760 330 880 420 1010 310 1100 370 1220 290 1340 380 1440 340V600H0Z"
        fill="url(#login-mid)"
      />
      <path
        d="M0 500 90 470 160 490 260 420 330 470 430 450 560 510 700 470 820 520 960 460 1080 500 1200 450 1330 500 1440 470V600H0Z"
        fill="url(#login-near)"
      />
      <path
        d="m340 300 22 22-22-8-14 14zM520 270l26 30-20-6-14 16zM1010 310l24 26-22-8-14 14zM1220 290l22 28-20-8-14 14z"
        fill="#cfe0ff"
        fillOpacity="0.16"
      />
    </svg>
  );
}
