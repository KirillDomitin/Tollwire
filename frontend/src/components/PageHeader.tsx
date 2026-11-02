import type { ReactNode } from "react";

import styles from "./PageHeader.module.css";

/** Шапка страницы: заголовок с подписью слева, действия (кнопки) справа. */
export function PageHeader({
  id,
  title,
  subtitle,
  actions,
}: {
  /** id заголовка — на него ссылается aria-labelledby раздела. */
  id: string;
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className={styles.header}>
      <div className={styles.titles}>
        <h1 id={id} className={styles.title}>
          {title}
        </h1>
        {subtitle && <p className={styles.subtitle}>{subtitle}</p>}
      </div>
      {actions && <div className={styles.actions}>{actions}</div>}
    </header>
  );
}
