import type { Schemas } from "./client";

export type PublicConfig = Schemas["PublicConfigRead"];

/** Причина, по которой страницу показать нельзя: человеку нужны лишь эти четыре исхода, а не подробности ответа. */
export type PublicConfigFailure = "invalid" | "tooMany" | "network" | "failed";

export class PublicConfigError extends Error {
  constructor(readonly failure: PublicConfigFailure) {
    super(failure);
  }
}

/**
 * Страница конфига открывается без входа. Токен идёт в теле POST (он взят из фрагмента адреса и ни в адрес запроса,
 * ни в логи не попадает); cookie и Authorization не отправляются; сессия админа и refresh не затрагиваются.
 * Любой 404 одинаков: по ответу не понять, был ли токен, заменён он или закончился доступ.
 */
export async function fetchPublicConfig(token: string, signal?: AbortSignal): Promise<PublicConfig> {
  let response: Response;
  try {
    response = await fetch(new URL("/api/v1/public/config", window.location.origin).href, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      credentials: "omit",
      cache: "no-store",
      signal,
    });
  } catch (exc) {
    if (exc instanceof DOMException && exc.name === "AbortError") throw exc;
    throw new PublicConfigError("network");
  }
  if (response.ok) return (await response.json()) as PublicConfig;
  if (response.status === 404 || response.status === 422) throw new PublicConfigError("invalid");
  if (response.status === 429) throw new PublicConfigError("tooMany");
  throw new PublicConfigError("failed");
}
