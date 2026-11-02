import type { ReactNode } from "react";

import styles from "./StatCard.module.css";

export type StatTone = "blue" | "green" | "red" | "violet" | "amber";

/** Плитка со сводкой: иконка в цветном квадрате, подпись и крупное значение. */
export function StatCard({
  icon,
  label,
  value,
  unit,
  tone = "blue",
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  /** Хвост значения: «/ 200», «%». */
  unit?: ReactNode;
  tone?: StatTone;
}) {
  return (
    <div className={styles.card} role="group" aria-label={label}>
      <span className={styles.icon} data-tone={tone}>
        {icon}
      </span>
      <div className={styles.body}>
        <span className={styles.label}>{label}</span>
        <span className={styles.value}>
          {value}
          {unit && <span className={styles.unit}>{unit}</span>}
        </span>
      </div>
    </div>
  );
}
