import createClient from "openapi-fetch";

import i18n from "../i18n";
import { authFetch } from "./session";
import type { components, paths } from "./schema";

/** Типизированный клиент API: пути, параметры и ответы — из OpenAPI-схемы бэкенда (npm run gen:api). */
// Явный origin, а не "": openapi-fetch строит new Request(url), и относительный адрес понимает только
// браузер (от адреса страницы). С origin запросы одинаковы и в браузере, и в тестах.
export const api = createClient<paths>({ baseUrl: window.location.origin, fetch: authFetch });

export type Schemas = components["schemas"];
export type Server = Schemas["ServerRead"];
export type Admin = Schemas["AdminRead"];
export type Job = Schemas["JobRead"];
export type Credential = Schemas["CredentialRead"];
export type HostKeyScan = Schemas["HostKeyScan"];
export type Protocol = Schemas["ProtocolRead"];
export type VPNUser = Schemas["VPNUserRead"];
export type VPNUserListItem = Schemas["VPNUserListItem"];
export type AccessSummary = Schemas["AccessSummary"];
export type Subscription = Schemas["SubscriptionRead"];
export type Grant = Schemas["AccessGrantRead"];
export type VPNCredential = Schemas["VPNCredentialRead"];

/** Ошибка API с понятным человеку текстом; status — для веток (401/403/429); detail — текст `detail` из ответа
 *  бэкенда (английский, фиксированные фразы): по нему выбирают, что показать, но сам он не показывается.
 *  reason — причина 409 (`no_telegram`, `not_configured`…), code — тип первой ошибки проверки в 422
 *  (`telegram_html_tag`…): по ним выбирают свой текст. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly retryAfterSeconds?: number,
    readonly detail?: string,
    readonly reason?: string,
    readonly code?: string,
  ) {
    super(message);
  }
}

export function messageForStatus(status: number): string {
  if (status === 401) return i18n.t("common.errors.sessionExpired");
  if (status === 403) return i18n.t("common.errors.forbidden");
  if (status === 429) return i18n.t("common.errors.tooManyRequests");
  if (status >= 500) return i18n.t("common.errors.server");
  return i18n.t("common.errors.requestFailed");
}

/** Достаёт данные из ответа openapi-fetch или бросает ApiError — удобно для TanStack Query.
 *  messages — понятный текст для конкретных статусов этого действия (бэкенд отвечает по-английски). */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
  messages: Partial<Record<number, string>> = {},
): Promise<T> {
  let result: { data?: T; error?: unknown; response: Response };
  try {
    result = await call;
  } catch {
    throw new ApiError(i18n.t("common.errors.network"), 0);
  }
  const { status, ok } = result.response;
  if (ok) return result.data as T; // 204 — данных нет, и это успех
  const body = result.error as { detail?: unknown; reason?: unknown } | null | undefined;
  const detail = body?.detail;
  const first: unknown = Array.isArray(detail) ? detail[0] : undefined;
  const type = (first as { type?: unknown } | undefined)?.type;
  // Retry-After читаем только у 429: «через сколько секунд повторить» нужно человеку, на остальных статусах заголовка нет.
  const retryAfter = status === 429 ? Number(result.response.headers.get("Retry-After")) : Number.NaN;
  throw new ApiError(
    messages[status] ?? messageForStatus(status),
    status,
    Number.isFinite(retryAfter) && retryAfter > 0 ? Math.ceil(retryAfter) : undefined,
    typeof detail === "string" ? detail : undefined,
    typeof body?.reason === "string" ? body.reason : undefined,
    typeof type === "string" ? type : undefined,
  );
}
