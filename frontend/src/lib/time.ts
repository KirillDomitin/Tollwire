const relative = new Intl.RelativeTimeFormat("ru", { numeric: "auto" });
const exact = new Intl.DateTimeFormat("ru", { dateStyle: "medium", timeStyle: "short" });

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
    if (Math.abs(value) < size) return relative.format(Math.round(value), unit);
    value /= size;
  }
  return relative.format(Math.round(value), "year");
}

export function exactTime(iso: string): string {
  return exact.format(new Date(iso));
}
