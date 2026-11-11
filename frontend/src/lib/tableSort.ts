import type { Server, VPNUserListItem } from "../api/client";
import { currentLanguage } from "../i18n/format";
import { accessPill } from "./access";

export type Sort<K extends string> = { field: K; direction: "ascending" | "descending" } | null;
export function nextSort<K extends string>(sort: Sort<K>, field: K): Sort<K> {
  return {
    field,
    direction: sort?.field === field && sort.direction === "ascending" ? "descending" : "ascending",
  };
}

/** Missing values stay last in both directions; ties always use ascending ID. */
export function sortRows<T extends { id: string }>(
  rows: readonly T[],
  direction: "ascending" | "descending",
  value: (row: T) => string | number | null | undefined,
): T[] {
  const collator = new Intl.Collator(currentLanguage(), { sensitivity: "accent" });
  const normalized = (row: T) => {
    const v = value(row);
    return v === "" || (typeof v === "number" && !Number.isFinite(v)) ? null : v;
  };
  return [...rows].sort((a, b) => {
    const av = normalized(a),
      bv = normalized(b);
    const id = a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    if (av == null) return bv == null ? id : 1;
    if (bv == null) return -1;
    const order =
      typeof av === "number" && typeof bv === "number" ? av - bv : collator.compare(String(av), String(bv));
    return (direction === "ascending" ? order : -order) || id;
  });
}

// Matches the displayed access states: active, ending, pending, expired, revoked; no access is missing.
export function accessRank(user: VPNUserListItem, now: Date): number | null {
  if (!user.access) return null;
  if (user.access.status === "revoked") return 4;
  const tone = accessPill(user.access, now).tone;
  return tone === "online" ? 0 : tone === "warning" ? 1 : tone === "process" ? 2 : 3;
}

// Running, degraded, unreachable, offline, error, installing, new, waiting for a check.
export function serverRank(server: Server): number {
  if (server.status === "ready") {
    return server.health_status === "ok"
      ? 0
      : server.health_status === "degraded"
        ? 1
        : server.health_status === "down"
          ? 2
          : 7;
  }
  return { offline: 3, error: 4, installing: 5, new: 6 }[server.status];
}
