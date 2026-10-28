import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../components/Button";
import { Notice } from "../../components/Notice";
import { errorMessage } from "./botErrors";
import styles from "./Bot.module.css";

/** Загрузка: одна и та же фраза во всех разделах, с aria-busy - экранный диктор сообщит, что данные в пути. */
export function Loading() {
  const { t } = useTranslation();
  return (
    <p className={styles.quiet} aria-busy="true">
      {t("bot.loading")}
    </p>
  );
}

/** Ошибка запроса: Notice с готовым текстом (английский `detail` бэкенда сюда не доходит) и «Загрузить снова». */
export function Problem({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { t } = useTranslation();
  return (
    <Notice
      action={
        onRetry && (
          <Button variant="quiet" onClick={onRetry}>
            {t("bot.reload")}
          </Button>
        )
      }
    >
      {errorMessage(error)}
    </Notice>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className={styles.quiet}>{children}</p>;
}

/** Первый упавший запрос раздела: раздел показывает одну ошибку вместо россыпи (и не мигает «Загрузка…» рядом). */
export function failed(...queries: { isError: boolean; error: unknown }[]) {
  return queries.find((query) => query.isError);
}
