import createClient from "openapi-fetch";

import { authFetch } from "./session";
import type { components, paths } from "./schema";

/** Типизированный клиент API: пути, параметры и ответы — из OpenAPI-схемы бэкенда (npm run gen:api). */
// Явный origin, а не "": openapi-fetch строит new Request(url), и относительный адрес понимает только
// браузер (от адреса страницы). С origin запросы одинаковы и в браузере, и в тестах.
export const api = createClient<paths>({ baseUrl: window.location.origin, fetch: authFetch });

export type Schemas = components["schemas"];
export type Server = Schemas["ServerRead"];
export type Admin = Schemas["AdminRead"];

/** Ошибка API с понятным человеку текстом; status — для веток (401/403/429). */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly retryAfterSeconds?: number,
  ) {
    super(message);
  }
}

export function messageForStatus(status: number): string {
  if (status === 401) return "Сессия закончилась. Войдите снова.";
  if (status === 403) return "Для этого действия у вашей роли нет прав.";
  if (status === 429) return "Слишком много запросов. Подождите немного и повторите.";
  if (status >= 500) return "Сервер панели ответил ошибкой. Повторите позже.";
  return "Запрос не выполнен.";
}

/** Достаёт данные из ответа openapi-fetch или бросает ApiError — удобно для TanStack Query. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  let result: { data?: T; error?: unknown; response: Response };
  try {
    result = await call;
  } catch {
    throw new ApiError("Нет связи с сервером панели. Проверьте подключение.", 0);
  }
  if (result.data !== undefined && result.response.ok) return result.data;
  throw new ApiError(messageForStatus(result.response.status), result.response.status);
}
