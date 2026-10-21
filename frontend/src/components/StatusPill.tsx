import type { ReactNode } from "react";

import type { Signal } from "../lib/serverSignal";
import styles from "./StatusPill.module.css";

export type PillTone = "online" | "offline" | "warning" | "process" | "neutral" | "violet";

/** Сигнал сервера (ok/warn/down/…) → тон пилюли. Одно правило на весь интерфейс. */
export const SIGNAL_TONE: Record<Signal, PillTone> = {
  ok: "online",
  down: "offline",
  warn: "warning",
  process: "process",
  idle: "neutral",
};

/** Пилюля статуса: цветная точка и подпись. Цвет дублируется текстом — по одному цвету состояние не читается. */
export function StatusPill({
  tone,
  children,
  dot = true,
}: {
  tone: PillTone;
  children: ReactNode;
  dot?: boolean;
}) {
  return (
    <span className={styles.pill} data-tone={tone}>
      {dot && <span className={styles.dot} aria-hidden="true" />}
      {children}
    </span>
  );
}
