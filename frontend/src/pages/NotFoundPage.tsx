import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { SearchIcon } from "../components/icons";
import styles from "./NotFoundPage.module.css";

export function NotFoundPage() {
  const { t } = useTranslation();
  return (
    <section className={styles.page} aria-labelledby="not-found-title">
      <span className={styles.icon} aria-hidden="true">
        <SearchIcon size={32} />
      </span>
      <h1 id="not-found-title" className={styles.title}>
        {t("shell.notFound.title")}
      </h1>
      <p className={styles.hint}>{t("shell.notFound.hint")}</p>
      <Link to="/servers" className={styles.link}>
        {t("shell.notFound.link")}
      </Link>
    </section>
  );
}
