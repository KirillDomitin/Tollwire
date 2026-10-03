import type { ReactNode } from "react";

import styles from "./Notice.module.css";

/** Сообщение об ошибке: что случилось и что делать. role=alert — экранный диктор зачитает сразу. */
export function Notice({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div role="alert" className={styles.notice}>
      <p>{children}</p>
      {action}
    </div>
  );
}
