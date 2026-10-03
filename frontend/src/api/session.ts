/**
 * Access-токен живёт только в памяти вкладки: не в localStorage (его прочитал бы любой внедрённый скрипт — XSS)
 * и не в cookie. После перезагрузки страницы он восстанавливается через refresh-cookie (HttpOnly — скрипту
 * недоступна, браузер сам прикладывает её к /api/v1/auth/*).
 */

let accessToken: string | null = null;
const listeners = new Set<(signedIn: boolean) => void>();

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
  listeners.forEach((listener) => listener(token !== null));
}

/** Подписка на «сессия появилась/пропала» — например, refresh не удался посреди работы. */
export function onSessionChange(listener: (signedIn: boolean) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

let refreshing: Promise<string | null> | null = null;

/**
 * Обновить access-токен по refresh-cookie. Один запрос на всех: если токен нужен сразу нескольким местам
 * (несколько запросов получили 401, или React в dev-режиме дважды запускает эффект), они ждут одного ответа.
 * Это важно: refresh-токен одноразовый — второй параллельный запрос с ним получил бы 401 и разлогинил бы.
 */
export function refreshAccessToken(): Promise<string | null> {
  refreshing ??= (async () => {
    try {
      const response = await fetch("/api/v1/auth/refresh", { method: "POST", credentials: "same-origin" });
      const token = response.ok ? ((await response.json()) as { access_token: string }).access_token : null;
      setAccessToken(token);
      return token;
    } catch {
      setAccessToken(null);
      return null;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

function withToken(request: Request, token: string | null): Request {
  const next = new Request(request);
  if (token) next.headers.set("Authorization", `Bearer ${token}`);
  return next;
}

/**
 * fetch для API: подставляет access-токен; на 401 один раз обновляет токен и повторяет запрос.
 * Повтор — копией исходного запроса (тело запроса читается один раз, поэтому копия делается заранее).
 */
export async function authFetch(input: Request): Promise<Response> {
  const retryCopy = input.clone();
  const response = await fetch(withToken(input, accessToken));
  if (response.status !== 401 || input.url.includes("/api/v1/auth/")) {
    return response;
  }
  const token = await refreshAccessToken();
  return token ? fetch(withToken(retryCopy, token)) : response;
}
