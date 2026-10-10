import type { AccessSummary } from "../api/client";
import { currentLanguage } from "../i18n/format";
import type { ServerSignal } from "./serverSignal";

/** Сколько дней до конца доступа считать «скоро кончится» — пора продлевать. */
export const EXPIRY_WARNING_DAYS = 7;

const DAY_MS = 24 * 60 * 60 * 1000;

/** «3 ноября» в текущем году, «3 ноября 2027 г.» — в другом. */
export function formatDay(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  const sameYear = date.getFullYear() === now.getFullYear();
  return new Intl.DateTimeFormat(currentLanguage(), {
    day: "numeric",
    month: "long",
    ...(sameYear ? {} : { year: "numeric" }),
  }).format(date);
}

type Period = Pick<AccessSummary, "status" | "valid_from" | "valid_until">;

/** Доступ «жив» — подписка ещё не завершена (ожидает начала или действует). */
export function isLive(period: Pick<Period, "status">): boolean {
  return period.status === "pending" || period.status === "active";
}

/**
 * Одно правило доступа на весь интерфейс — тем же языком полос, что у серверов.
 * Зелёное — действует; янтарное — кончится в ближайшие дни; синее — ещё не началось; серое — доступа нет.
 */
export function accessSignal(access: Period | null, now: Date = new Date()): ServerSignal {
  if (!access) return { signal: "idle", label: "Нет доступа" };
  if (access.status === "revoked") return { signal: "idle", label: "Отозван" };
  const until = new Date(access.valid_until);
  if (access.status === "expired" || until <= now) {
    return { signal: "idle", label: `Истёк ${formatDay(access.valid_until, now)}` };
  }
  // pending с уже начавшимся периодом — планировщик просто ещё не перевёл его в active: для человека он «действует».
  if (access.status === "pending" && new Date(access.valid_from) > now) {
    return { signal: "process", label: `С ${formatDay(access.valid_from, now)}` };
  }
  if (until.getTime() - now.getTime() <= EXPIRY_WARNING_DAYS * DAY_MS) {
    return { signal: "warn", label: `Кончается ${formatDay(access.valid_until, now)}` };
  }
  return { signal: "ok", label: `До ${formatDay(access.valid_until, now)}` };
}
