import type { Server } from "../api/client";
import i18n from "../i18n";

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
      return { signal: "down", label: i18n.t("servers.signal.error") };
    case "offline":
      return { signal: "down", label: i18n.t("servers.signal.offline") };
    case "installing":
      return { signal: "process", label: i18n.t("servers.signal.installing") };
    case "new":
      return { signal: "idle", label: i18n.t("servers.signal.new") };
    case "ready":
      if (server.health_status === "down") return { signal: "down", label: i18n.t("servers.signal.down") };
      if (server.health_status === "degraded")
        return { signal: "warn", label: i18n.t("servers.signal.degraded") };
      if (server.health_status === "ok") return { signal: "ok", label: i18n.t("servers.signal.ok") };
      return { signal: "idle", label: i18n.t("servers.signal.waiting") };
  }
}

const GROUP_ORDER: Signal[] = ["down", "warn", "process", "ok", "idle"]; // сначала то, что требует внимания

/** Сводка одной фразой: «4 сервера — 1 недоступен, 3 в работе». */
export function fleetSummary(servers: Pick<Server, "status" | "health_status">[]): string {
  if (servers.length === 0) return i18n.t("servers.fleet.none");
  const counts = new Map<Signal, number>();
  for (const server of servers) {
    const { signal } = serverSignal(server);
    counts.set(signal, (counts.get(signal) ?? 0) + 1);
  }
  const total = i18n.t("servers.fleet.total", { count: servers.length });
  if (counts.size === 1) {
    // Все в одном состоянии: «3 сервера — все в работе», «1 сервер — недоступен».
    const [signal] = [...counts.keys()] as [Signal];
    const state =
      servers.length === 1
        ? i18n.t(`servers.fleet.${signal}`, { count: 1 })
        : i18n.t("servers.fleet.all", { state: i18n.t(`servers.fleet.${signal}`, { count: 2 }) });
    return `${total} — ${state}`;
  }
  const parts = GROUP_ORDER.filter((signal) => counts.has(signal)).map((signal) => {
    const count = counts.get(signal) ?? 0;
    return `${count} ${i18n.t(`servers.fleet.${signal}`, { count })}`;
  });
  return `${total} — ${parts.join(", ")}`;
}
