import { useTranslation } from "react-i18next";

import { useAuth } from "../../auth/AuthContext";
import { BroadcastForm } from "./BroadcastForm";
import { BroadcastList } from "./BroadcastList";
import styles from "./Bot.module.css";

/** «Рассылки» (/bot/broadcasts): форма новой рассылки (bot:manage) и список со счётчиками. */
export function BroadcastsPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  return (
    <section className={styles.section} aria-labelledby="bot-broadcasts-title">
      <h2 id="bot-broadcasts-title" className={styles.sectionTitle}>
        {t("bot.broadcasts.title")}
      </h2>
      {can("bot:manage") && <BroadcastForm />}
      <BroadcastList />
    </section>
  );
}
