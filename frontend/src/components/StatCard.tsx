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
  action,
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  /** Хвост значения: «/ 200», «%». */
  unit?: ReactNode;
  tone?: StatTone;
  action?: { pressed: boolean; onClick: () => void };
}) {
  const content = (
    <>
      <span className={styles.icon} data-tone={tone} aria-hidden="true">
        {icon}
      </span>
      <span className={styles.body}>
        <span className={styles.label}>{label}</span>
        <span className={styles.value}>
          {value}
          {unit && <span className={styles.unit}>{unit}</span>}
        </span>
      </span>
    </>
  );
  if (action) {
    return (
      <button
        type="button"
        className={styles.card}
        aria-label={label}
        aria-pressed={action.pressed}
        onClick={action.onClick}
      >
        {content}
      </button>
    );
  }
  return (
    <div className={styles.card} role="group" aria-label={label}>
      {content}
    </div>
  );
}
