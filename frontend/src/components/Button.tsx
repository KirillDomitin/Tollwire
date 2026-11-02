import type { ButtonHTMLAttributes, ReactNode } from "react";

import styles from "./Button.module.css";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** primary — главное действие (синяя), secondary — рядовое, danger — разрушающее, ghost — без рамки. */
  variant?: "primary" | "secondary" | "danger" | "ghost";
  size?: "md" | "sm";
  /** Иконка слева от текста (декоративная — подпись остаётся текстом). */
  icon?: ReactNode;
  /** Идёт действие: кнопка недоступна, текст сообщает, что происходит. */
  busyLabel?: string;
  busy?: boolean;
}

export function Button({
  variant = "primary",
  size = "md",
  icon,
  busy,
  busyLabel,
  children,
  disabled,
  className,
  ...rest
}: ButtonProps) {
  return (
    <button
      type="button"
      className={[styles.button, styles[variant], size === "sm" ? styles.small : "", className ?? ""]
        .filter(Boolean)
        .join(" ")}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {busy ? <span className={styles.spinner} aria-hidden="true" /> : icon}
      {busy && busyLabel ? busyLabel : children}
    </button>
  );
}
