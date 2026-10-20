import type { Schemas } from "../../../api/client";
import i18n from "../../../i18n";

export type GatewayExit = Schemas["GatewayExitRead"];
export type GatewayAssignment = Schemas["GatewayAssignmentRead"];
export type GeoipList = Schemas["GeoipListRead"];
export type GeoipOverride = Schemas["OverrideRead"];
export type GeoipIngest = Schemas["GeoipIngestResult"];
export type AssignReason = Schemas["AssignReason"];

/** Подсеть клиентов шлюза зашита в провайдере бэкенда (infrastructure/vpn/gateway_config.py) и не настраивается. */
export const GATEWAY_SUBNET = "10.77.0.0/22";

/** Границы полей — как в схемах бэкенда (gateway/schemas.py). */
export const WEIGHT_MIN = 1;
export const WEIGHT_MAX = 100;
export const MAX_CLIENTS_LIMIT = 100_000;
export const NOTE_MAX = 100;

/** Файл списка для загрузки: бэкенд принимает до 5 МБ (GEOIP_UPLOAD_MAX_BYTES), больше — 413. */
export const UPLOAD_MAX_BYTES = 5 * 1024 * 1024;

/** По сколько строк показываются длинные списки (назначения клиентов). */
export const PAGE_SIZE = 25;

export type Tone = "ok" | "warn" | "down" | "process" | "idle";

export interface ExitView {
  tone: Tone;
  /** Ключ подписи состояния в `gateway.exits.states`. */
  state: "active" | "connecting" | "removing" | "error" | "notConnected" | "notRemoved";
  /** Добавление или снятие не закончилось, а задачи уже нет: действие можно повторить. */
  stalled: "add" | "remove" | null;
}

/**
 * Состояние выхода одним словом для человека. Состояние в БД (pending / peer_created / draining / removing) говорит,
 * что задача начата, но не идёт ли она сейчас — по нему не понять: упавшая задача оставляет то же состояние. Поэтому
 * учитывается, идёт ли задача добавления / снятия на самом деле.
 */
export function exitView(exit: GatewayExit, running: { adding: boolean; removing: boolean }): ExitView {
  switch (exit.state) {
    case "pending":
    case "peer_created":
      return running.adding
        ? { tone: "process", state: "connecting", stalled: null }
        : { tone: "down", state: "notConnected", stalled: "add" };
    case "draining":
    case "removing":
      return running.removing
        ? { tone: "process", state: "removing", stalled: null }
        : { tone: "down", state: "notRemoved", stalled: "remove" };
    case "active":
      // down и stale балансировщик считает отказом (клиентов с такого выхода переносят); нет рукопожатия — пока только повод
      // присмотреться: туннель мог не успеть подняться.
      if (exit.health === "down" || exit.health === "stale")
        return { tone: "down", state: "error", stalled: null };
      if (exit.health === "no_handshake") return { tone: "warn", state: "active", stalled: null };
      return { tone: "ok", state: "active", stalled: null };
  }
}

/** Подпись здоровья выхода; незнакомое значение бэкенда показываем как есть, а не выдумываем. */
export function healthLabel(health: string | null): string {
  if (health === null) return i18n.t("gateway.health.unknown");
  const known = ["ok", "stale", "no_handshake", "down"] as const;
  return (known as readonly string[]).includes(health)
    ? i18n.t(`gateway.health.${health as (typeof known)[number]}`)
    : health;
}

export function reasonLabel(reason: AssignReason | string): string {
  const known = ["issue", "drain", "failover", "failover_return", "manual", "sync", "pin", "unpin"] as const;
  return (known as readonly string[]).includes(reason)
    ? i18n.t(`gateway.reasons.${reason as (typeof known)[number]}`)
    : reason;
}

/**
 * 409 на снятие выхода: «N client(s) of this exit would be left without an exit and without internet; ...».
 * Число клиентов — из текста: других полей в ответе нет. Другой 409 (задача идёт, выхода нет) — null.
 */
export function clientsLeft(detail: string | undefined): number | null {
  const match = /^(\d+) client\(s\) of this exit would be left without an exit/.exec(detail ?? "");
  return match ? Number(match[1]) : null;
}

/** Целое в заданных границах из поля формы; пусто, дробь, буквы и выход за границы — null. */
export function parseWhole(text: string, min: number, max: number): number | null {
  const value = text.trim();
  if (!/^\d+$/.test(value)) return null;
  const number = Number(value);
  return number >= min && number <= max ? number : null;
}

const CIDR = /^\d{1,3}(\.\d{1,3}){3}\/\d{1,2}$/;

/** Сеть похожа на «5.45.200.0/24»: остальное (биты хоста, публичность, размер) проверяет бэкенд. */
export function looksLikeCidr(text: string): boolean {
  return CIDR.test(text.trim());
}

/**
 * Понятный текст отказа бэкенда (422) для списка и исключений «России напрямую». Бэкенд отвечает фиксированными
 * английскими фразами - известные переводим, остальное - общий текст: сырую фразу пользователю не показываем.
 */
export function geoipErrorText(detail: string | undefined): string {
  const text = detail ?? "";
  if (text.startsWith("Network must be an IPv4 CIDR with the host bits zero"))
    return i18n.t("gateway.ru.errors.hostBits");
  if (text.startsWith("Network must be an IPv4 CIDR")) return i18n.t("gateway.ru.errors.format");
  if (text.startsWith("A direct network must be public")) return i18n.t("gateway.ru.errors.notPublic");
  if (text.startsWith("A tunnel network must not be larger")) return i18n.t("gateway.ru.errors.tunnelTooBig");
  if (text.includes("must not cover the address of a gateway or an exit"))
    return i18n.t("gateway.ru.errors.coversServer");
  if (text.startsWith("The list contains a network that cannot be routed directly"))
    return i18n.t("gateway.ru.errors.listReserved");
  if (text.startsWith("Too few valid records")) return i18n.t("gateway.ru.errors.tooFew");
  const size = /^The list has (\d+) prefixes after merging, expected from (\d+) to (\d+)/.exec(text);
  if (size) return i18n.t("gateway.ru.errors.size", { prefixes: size[1], min: size[2], max: size[3] });
  return i18n.t("gateway.ru.errors.rejected");
}

/**
 * Итог получения списка одной фразой (загрузка файла и задача «Обновить из RIPE» отдают одно и то же):
 * принят и активен, принят кандидатом и ждёт подтверждения, или совпал с активным.
 */
export function ingestMessage(result: {
  status: string;
  version?: unknown;
  prefixes?: unknown;
  rejected?: unknown;
}): string {
  const values = { version: String(result.version ?? "—"), prefixes: String(result.prefixes ?? "—") };
  const rejected =
    typeof result.rejected === "number" && result.rejected > 0
      ? ` ${i18n.t("gateway.ru.upload.rejected", { count: result.rejected })}`
      : "";
  switch (result.status) {
    case "candidate":
      return i18n.t("gateway.ru.upload.candidate", values) + rejected;
    case "unchanged":
      return i18n.t("gateway.ru.upload.unchanged", values) + rejected;
    default:
      return i18n.t("gateway.ru.upload.active", values) + rejected;
  }
}
