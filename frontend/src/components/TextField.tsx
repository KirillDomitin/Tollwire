import { useId, type InputHTMLAttributes, type ReactNode } from "react";

import styles from "./TextField.module.css";

interface TextFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  /** Что не так со значением: показывается под полем и читается экранным диктором вместе с ним. */
  error?: string | null;
  /** Иконка слева внутри поля (декоративная). */
  icon?: ReactNode;
  /** Элемент справа внутри поля — например, кнопка «показать пароль». */
  trailing?: ReactNode;
  /** Подпись только для экранного диктора — когда смысл поля ясен из иконки и подсказки внутри. */
  hideLabel?: boolean;
}

export function TextField({ label, error, icon, trailing, hideLabel, className, ...input }: TextFieldProps) {
  const id = useId();
  const errorId = `${id}-error`;
  return (
    <div className={`${styles.field} ${className ?? ""}`}>
      <label htmlFor={id} className={hideLabel ? "visually-hidden" : styles.label}>
        {label}
      </label>
      <div className={styles.control}>
        {icon && <span className={styles.icon}>{icon}</span>}
        <input
          id={id}
          className={styles.input}
          data-icon={icon ? "true" : undefined}
          data-trailing={trailing ? "true" : undefined}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          {...input}
        />
        {trailing && <span className={styles.trailing}>{trailing}</span>}
      </div>
      {error && (
        <p id={errorId} className={styles.error}>
          {error}
        </p>
      )}
    </div>
  );
}
