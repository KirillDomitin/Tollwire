import { memoByLanguage } from "../i18n/format";

const relative = memoByLanguage((lng) => new Intl.RelativeTimeFormat(lng, { numeric: "auto" }));
const exact = memoByLanguage(
  (lng) => new Intl.DateTimeFormat(lng, { dateStyle: "medium", timeStyle: "short" }),
);

const STEPS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["second", 60],
  ["minute", 60],
  ["hour", 24],
  ["day", 30],
  ["month", 12],
];

/** «2 минуты назад», «вчера». Точное время — в exactTime (для подсказки). */
export function timeAgo(iso: string, now: Date = new Date()): string {
  let value = (new Date(iso).getTime() - now.getTime()) / 1000;
  for (const [unit, size] of STEPS) {
    if (Math.abs(value) < size) return relative().format(Math.round(value), unit);
    value /= size;
  }
  return relative().format(Math.round(value), "year");
}

export function exactTime(iso: string): string {
  return exact().format(new Date(iso));
}

/** Календарный день по местному времени в виде «2026-10-31» — значение для `<input type="date">`. */
export function localDay(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** Сдвиг на целое число календарных дней по местному времени (переход на летнее время день не съедает). */
export function addDays(date: Date, days: number): Date {
  const shifted = new Date(date);
  shifted.setDate(shifted.getDate() + days);
  return shifted;
}

/**
 * Строка — настоящий календарный день «2026-10-31» (не «20261-01-01», не «2026-02-31»). Сверка круговая: день
 * разбирается и собирается обратно, а непохожая на себя дата (31 февраля -> 3 марта) отбрасывается.
 */
export function isValidDay(day: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
  const parsed = new Date(`${day}T00:00:00`);
  return !Number.isNaN(parsed.getTime()) && localDay(parsed) === day;
}

/** Конец выбранного дня («2026-10-31») по местному времени — как ISO-момент с часовым поясом (UTC, «Z»). */
export function endOfLocalDay(day: string): string {
  return new Date(`${day}T23:59:59`).toISOString();
}
