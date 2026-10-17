import type { Schemas } from "../../api/client";
import i18n from "../../i18n";

export type DiscoveredProtocol = Schemas["DiscoveredProtocolRead"];
export type DiscoveredClient = Schemas["DiscoveredClientRead"];
export type ImportResult = {
  imported?: unknown[];
  already?: unknown[];
  gone?: unknown[];
  unsupported?: unknown[];
  conflict?: { public_key: string; reason: string }[];
};

/** Установки приложения AmneziaVPN: v1 (контейнер amnezia-awg) и AWG 2.0 (amnezia-awg2). */
export const APP_PROTOCOL_TYPES = ["amnezia_app_awg", "amnezia_app_awg2"] as const;
export type AppProtocolType = (typeof APP_PROTOCOL_TYPES)[number];

/** Сколько клиентов принимает один запрос переноса (как на бэкенде). */
export const IMPORT_MAX_CLIENTS = 250;

export type AppProtocol = DiscoveredProtocol & { type: AppProtocolType };

export function isAppProtocol(protocol: DiscoveredProtocol): protocol is AppProtocol {
  return (APP_PROTOCOL_TYPES as readonly string[]).includes(protocol.type);
}

/** Клиенты, которых можно подхватить: новые и когда-то отозванные в панели. Остальные недоступны. */
export function isImportable(client: DiscoveredClient): boolean {
  return client.status === "new" || client.status === "revoked_in_panel";
}

export function newClientsCount(protocol: DiscoveredProtocol): number {
  return protocol.clients.filter((client) => client.status === "new").length;
}

export function importableCount(protocol: DiscoveredProtocol): number {
  return protocol.clients.filter(isImportable).length;
}

/**
 * Текст отказа из итога переноса. Бэкенд отвечает фиксированными английскими фразами - известные переводим,
 * незнакомую показываем как есть, а не выдумываем.
 */
const REASON_KEYS: Record<
  string,
  "userHasAccess" | "emailTaken" | "userNotFound" | "notInEffect" | "inPast" | "dataConflict"
> = {
  "User already has an access to this server and protocol": "userHasAccess",
  "User with this email already exists": "emailTaken",
  "User not found": "userNotFound",
  "Subscription of the user is not in effect now": "notInEffect",
  "valid_until is already in the past": "inPast",
  "Conflicts with existing data": "dataConflict",
};

export function conflictReason(reason: string): string {
  const key = REASON_KEYS[reason];
  return key ? i18n.t(`servers.import.reasons.${key}`) : reason;
}

/** Итог переноса одной строкой-списком: только ненулевые счётчики («Подхвачено: 3», «Пропали с сервера: 1»). */
export function importSummary(result: ImportResult | null | undefined): string[] {
  if (!result) return [];
  const counts = {
    imported: result.imported?.length ?? 0,
    already: result.already?.length ?? 0,
    gone: result.gone?.length ?? 0,
    unsupported: result.unsupported?.length ?? 0,
    conflict: result.conflict?.length ?? 0,
  };
  return (Object.keys(counts) as (keyof typeof counts)[])
    .filter((name) => counts[name] > 0)
    .map((name) => i18n.t(`servers.import.result.${name}`, { count: counts[name] }));
}
