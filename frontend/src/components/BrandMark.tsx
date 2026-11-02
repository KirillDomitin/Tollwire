import { useId } from "react";

import styles from "./BrandMark.module.css";

/** Знак WirePilot: щит с буквой W в синем градиенте. */
export function ShieldMark({ size = 32 }: { size?: number }) {
  const id = useId();
  return (
    <svg
      className={styles.shield}
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id={`${id}-g`} x1="6" y1="3" x2="26" y2="29" gradientUnits="userSpaceOnUse">
          <stop stopColor="#5aa2ff" />
          <stop offset="1" stopColor="#1a5cff" />
        </linearGradient>
      </defs>
      <path
        d="M16 2.8 5 7v8.2c0 6.3 4.3 11.2 11 14 6.7-2.8 11-7.7 11-14V7L16 2.8Z"
        fill="#0d1a38"
        stroke={`url(#${id}-g)`}
        strokeWidth="2"
        strokeLinejoin="round"
      />
      <path
        d="m10.2 11.4 2.2 8.6 2.4-6 1.2 0 2.4 6 2.2-8.6"
        stroke={`url(#${id}-g)`}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Логотип: щит с W и название продукта. */
export function BrandMark({ name = true, size = 32 }: { name?: boolean; size?: number }) {
  return (
    <span className={styles.brand}>
      <ShieldMark size={size} />
      {name && <span className={styles.name}>WirePilot</span>}
    </span>
  );
}
