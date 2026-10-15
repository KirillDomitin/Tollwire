import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api, ApiError, messageForStatus, type Admin } from "../api/client";
import i18n from "../i18n";
import { getAccessToken, onSessionChange, refreshAccessToken, setAccessToken } from "../api/session";

/** Как на бэкенде: MIN_PASSWORD_LENGTH. */
export const MIN_PASSWORD_LENGTH = 12;

type AuthState = { status: "checking" } | { status: "signed-out" } | { status: "signed-in"; admin: Admin };

interface AuthApi {
  state: AuthState;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Сменить свой пароль. Остальные сессии завершаются, текущая получает новый токен; профиль перечитывается. */
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
  /** Есть ли у текущего админа право (строка из /auth/me, например "servers:manage"). */
  can: (permission: string) => boolean;
}

// Экспорт — для тестов: экран можно отрисовать с заданными правами, не проходя вход.
export const AuthContext = createContext<AuthApi | null>(null);

async function loadMe(): Promise<Admin> {
  const { data, response } = await api.GET("/api/v1/auth/me");
  if (!data) throw new ApiError(messageForStatus(response.status), response.status);
  return data;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "checking" });
  const queryClient = useQueryClient();

  // Открыли страницу: сессию восстанавливаем тихо — по refresh-cookie, без формы входа.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const token = await refreshAccessToken();
      const admin = token ? await loadMe().catch(() => null) : null;
      if (!cancelled) setState(admin ? { status: "signed-in", admin } : { status: "signed-out" });
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Refresh не удался посреди работы (сессию завершили, админа отключили) — на экран входа.
  useEffect(
    () =>
      onSessionChange((signedIn) => {
        if (!signedIn) {
          setState((current) => (current.status === "signed-in" ? { status: "signed-out" } : current));
        }
      }),
    [],
  );

  const login = useCallback(async (email: string, password: string) => {
    let response: Response;
    try {
      response = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
        credentials: "same-origin",
      });
    } catch {
      throw new ApiError(i18n.t("common.errors.network"), 0);
    }
    if (!response.ok) {
      const retryAfter = Number(response.headers.get("Retry-After")) || undefined;
      throw new ApiError(loginErrorMessage(response.status, retryAfter), response.status, retryAfter);
    }
    const { access_token } = (await response.json()) as { access_token: string };
    setAccessToken(access_token);
    setState({ status: "signed-in", admin: await loadMe() });
  }, []);

  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    // Не через openapi-fetch: ответ 401 для /auth/* там не повторяется после refresh, а здесь это нужно —
    // админ с временным паролем мог просидеть над формой дольше жизни access-токена.
    const send = () => {
      const token = getAccessToken();
      return fetch("/api/v1/auth/change-password", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
        credentials: "same-origin",
      });
    };
    let response: Response;
    try {
      response = await send();
      if (response.status === 401 && (await refreshAccessToken())) response = await send();
    } catch {
      throw new ApiError(i18n.t("common.errors.network"), 0);
    }
    if (!response.ok) {
      const retryAfter = Number(response.headers.get("Retry-After")) || undefined;
      throw new ApiError(
        changePasswordErrorMessage(response.status, retryAfter),
        response.status,
        retryAfter,
      );
    }
    const { access_token } = (await response.json()) as { access_token: string };
    setAccessToken(access_token);
    setState({ status: "signed-in", admin: await loadMe() });
  }, []);

  const logout = useCallback(async () => {
    await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" }).catch(() => undefined);
    setAccessToken(null);
    queryClient.clear(); // данные прошлого админа не должны мелькнуть следующему
    setState({ status: "signed-out" });
  }, [queryClient]);

  const value = useMemo<AuthApi>(
    () => ({
      state,
      login,
      logout,
      changePassword,
      can: (permission) => state.status === "signed-in" && state.admin.permissions.includes(permission),
    }),
    [state, login, logout, changePassword],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function loginErrorMessage(status: number, retryAfterSeconds?: number): string {
  if (status === 401) return i18n.t("auth.errors.invalidCredentials");
  if (status === 422) return i18n.t("auth.errors.incompleteEmail");
  if (status === 429) {
    const minutes = retryAfterSeconds ? Math.max(1, Math.ceil(retryAfterSeconds / 60)) : null;
    return minutes
      ? i18n.t("auth.errors.tooManyAttemptsMinutes", { minutes })
      : i18n.t("auth.errors.tooManyAttempts");
  }
  return messageForStatus(status);
}

export function changePasswordErrorMessage(status: number, retryAfterSeconds?: number): string {
  // 400 — неверный текущий пароль (не 401: 401 означает «сессия закончилась»).
  if (status === 400) return i18n.t("auth.changePassword.errors.wrongCurrent");
  if (status === 422) return i18n.t("auth.changePassword.errors.invalidNew", { min: MIN_PASSWORD_LENGTH });
  if (status === 429) {
    const minutes = retryAfterSeconds ? Math.max(1, Math.ceil(retryAfterSeconds / 60)) : null;
    return minutes
      ? i18n.t("auth.changePassword.errors.tooManyAttemptsMinutes", { minutes })
      : i18n.t("auth.changePassword.errors.tooManyAttempts");
  }
  return messageForStatus(status);
}

export function useAuth(): AuthApi {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside <AuthProvider>");
  return context;
}
