import type { Server } from "../api/client";

/** Сигнал строки сервера: цвет статусной полосы и короткая подпись состояния. */
export type Signal = "ok" | "warn" | "down" | "process" | "idle";

export interface ServerSignal {
  signal: Signal;
  label: string;
}

/**
 * Одно правило на весь интерфейс: из статуса сервера (жизненный цикл установки) и итога health-проверки.
 * Статус важнее: ошибка установки или недоступность — красное, даже если старая проверка была «ok».
 */
export function serverSignal(server: Pick<Server, "status" | "health_status">): ServerSignal {
  switch (server.status) {
    case "error":
      return { signal: "down", label: "Ошибка" };
    case "offline":
      return { signal: "down", label: "Недоступен" };
    case "installing":
      return { signal: "process", label: "Устанавливается" };
    case "new":
      return { signal: "idle", label: "Не настроен" };
    case "ready":
      if (server.health_status === "down") return { signal: "down", label: "Не отвечает" };
      if (server.health_status === "degraded") return { signal: "warn", label: "Есть проблемы" };
      if (server.health_status === "ok") return { signal: "ok", label: "В работе" };
      return { signal: "idle", label: "Ждёт проверки" };
  }
}

const plural = new Intl.PluralRules("ru");

/** Форма слова по числу: pick(5, ["сервер", "сервера", "серверов"]) → "серверов". */
export function pick(count: number, [one, few, many]: [string, string, string]): string {
  const category = plural.select(count);
  return category === "one" ? one : category === "few" ? few : many;
}

const GROUP_WORDS: Record<Signal, [string, string, string]> = {
  ok: ["в работе", "в работе", "в работе"],
  warn: ["с проблемами", "с проблемами", "с проблемами"],
  down: ["недоступен", "недоступны", "недоступны"],
  process: ["устанавливается", "устанавливаются", "устанавливаются"],
  idle: ["не настроен", "не настроены", "не настроены"],
};
const GROUP_ORDER: Signal[] = ["down", "warn", "process", "ok", "idle"]; // сначала то, что требует внимания

/** Сводка одной фразой: «4 сервера — 1 недоступен, 3 в работе». */
export function fleetSummary(servers: Pick<Server, "status" | "health_status">[]): string {
  if (servers.length === 0) return "Серверов пока нет";
  const counts = new Map<Signal, number>();
  for (const server of servers) {
    const { signal } = serverSignal(server);
    counts.set(signal, (counts.get(signal) ?? 0) + 1);
  }
  const total = `${servers.length} ${pick(servers.length, ["сервер", "сервера", "серверов"])}`;
  if (counts.size === 1) {
    // Все в одном состоянии: «3 сервера — все в работе», «1 сервер — недоступен».
    const [signal] = [...counts.keys()] as [Signal];
    return servers.length === 1
      ? `${total} — ${GROUP_WORDS[signal][0]}`
      : `${total} — все ${GROUP_WORDS[signal][1]}`;
  }
  const parts = GROUP_ORDER.filter((signal) => counts.has(signal)).map((signal) => {
    const count = counts.get(signal) ?? 0;
    return `${count} ${pick(count, GROUP_WORDS[signal])}`;
  });
  return `${total} — ${parts.join(", ")}`;
}
