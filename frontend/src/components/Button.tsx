import type { ButtonHTMLAttributes } from "react";

import styles from "./Button.module.css";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "quiet";
  /** Идёт действие: кнопка недоступна, текст сообщает, что происходит. */
  busyLabel?: string;
  busy?: boolean;
}

export function Button({ variant = "primary", busy, busyLabel, children, disabled, ...rest }: ButtonProps) {
  return (
    <button
      type="button"
      className={`${styles.button} ${styles[variant]}`}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {busy && busyLabel ? busyLabel : children}
    </button>
  );
}
