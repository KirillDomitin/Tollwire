import type { AccessSummary } from "../api/client";
import i18n from "../i18n";
import { currentLanguage } from "../i18n/format";
import type { PillTone } from "../components/StatusPill";
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
  if (!access) return { signal: "idle", label: i18n.t("users.access.none") };
  if (access.status === "revoked") return { signal: "idle", label: i18n.t("users.access.revoked") };
  const until = new Date(access.valid_until);
  if (access.status === "expired" || until <= now) {
    return {
      signal: "idle",
      label: i18n.t("users.access.expired", { date: formatDay(access.valid_until, now) }),
    };
  }
  // pending с уже начавшимся периодом — планировщик просто ещё не перевёл его в active: для человека он «действует».
  if (access.status === "pending" && new Date(access.valid_from) > now) {
    return {
      signal: "process",
      label: i18n.t("users.access.from", { date: formatDay(access.valid_from, now) }),
    };
  }
  if (until.getTime() - now.getTime() <= EXPIRY_WARNING_DAYS * DAY_MS) {
    return {
      signal: "warn",
      label: i18n.t("users.access.ending", { date: formatDay(access.valid_until, now) }),
    };
  }
  return { signal: "ok", label: i18n.t("users.access.until", { date: formatDay(access.valid_until, now) }) };
}

export interface AccessPill {
  tone: PillTone;
  label: string;
}

/** Состояние подписки для пилюли: «Действует», «Кончается», «Истёк», «Отозван», «Нет доступа». Даты — в соседнем столбце. */
export function accessPill(access: Period | null, now: Date = new Date()): AccessPill {
  const label = (key: string) => i18n.t(`users.access.pill.${key}` as "users.access.pill.active");
  if (!access) return { tone: "neutral", label: label("none") };
  if (access.status === "revoked") return { tone: "neutral", label: label("revoked") };
  const { signal } = accessSignal(access, now);
  if (access.status === "expired" || new Date(access.valid_until) <= now) {
    return { tone: "offline", label: label("expired") };
  }
  if (signal === "process") return { tone: "process", label: label("pending") };
  if (signal === "warn") return { tone: "warning", label: label("ending") };
  return { tone: "online", label: label("active") };
}

/** Сколько целых дней осталось до конца доступа («осталось 30 дней»); доступ кончился или отозван — null. */
export function daysLeft(access: Period | null, now: Date = new Date()): string | null {
  if (!access || access.status === "revoked" || access.status === "expired") return null;
  const ms = new Date(access.valid_until).getTime() - now.getTime();
  if (ms <= 0) return null;
  return i18n.t("users.access.daysLeft", { count: Math.ceil(ms / DAY_MS) });
}
