import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, Outlet, useLocation } from "react-router";

import { useAuth } from "../../auth/AuthContext";
import styles from "./Bot.module.css";
import { Loading, Problem } from "./states";
import { useBotStatus } from "./useBot";

const SECTIONS = [
  { to: "/bot", key: "status", end: true },
  { to: "/bot/invites", key: "invites", end: false },
  { to: "/bot/registrations", key: "registrations", end: false },
  { to: "/bot/broadcasts", key: "broadcasts", end: false },
  { to: "/bot/settings", key: "settings", end: false },
] as const;

const isOpen = (section: (typeof SECTIONS)[number], pathname: string) =>
  section.end ? pathname === section.to : pathname === section.to || pathname.startsWith(`${section.to}/`);

/** Чанки разделов подгружаются, как только открыт каркас: переход между вкладками не ждёт сети. */
const PRELOAD = [
  () => import("./StatusPage"),
  () => import("./InvitesPage"),
  () => import("./RegistrationsPage"),
  () => import("./BroadcastsPage"),
  () => import("./SettingsPage"),
];

/**
 * Каркас раздела «Бот»: заголовок, ссылки на подразделы и сам подраздел. Пока ключа бота нет, подразделов нет: не-суперадмину
 * говорят «Бот не подключён» (история и рассылки без бота бессмысленны), а суперадмину оставляют всё - иначе первый ключ
 * из панели не создать. «Есть ключ» - любой, в том числе отозванный: история регистраций остаётся доступной.
 */
export function BotPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const status = useBotStatus();
  const connected = status.data?.has_keys === true || can("bot:keys");
  const { pathname } = useLocation();
  // Нажатая вкладка отмечается сразу, не дожидаясь загрузки её чанка; пока адрес прежний, пометка действует.
  const [clicked, setClicked] = useState<{ from: string; to: string } | null>(null);
  const current = clicked && clicked.from === pathname ? clicked.to : pathname;

  // Вкладки показываются, когда их чанки уже на месте (не вышло загрузить - всё равно показываются): нажатие не ждёт сети.
  const [preloaded, setPreloaded] = useState(false);
  useEffect(() => {
    void Promise.allSettled(PRELOAD.map((load) => load())).then(() => setPreloaded(true));
  }, []);

  return (
    <section className={styles.page} aria-labelledby="bot-title">
      <h1 id="bot-title" className={styles.title}>
        {t("bot.title")}
      </h1>
      {(status.isPending || (status.data && !preloaded)) && <Loading />}
      {status.isError && <Problem error={status.error} onRetry={() => void status.refetch()} />}
      {status.data && preloaded && !connected && (
        <div className={styles.section}>
          <h2 className={styles.subTitle}>{t("bot.notConnected.title")}</h2>
          <p className={styles.quiet}>{t("bot.notConnected.hint")}</p>
        </div>
      )}
      {status.data && preloaded && connected && (
        <>
          <nav aria-label={t("bot.nav.label")} className={styles.nav}>
            {SECTIONS.map((section) => (
              <Link
                key={section.to}
                to={section.to}
                className={styles.navLink}
                aria-current={isOpen(section, current) ? "page" : undefined}
                onClick={() => setClicked({ from: pathname, to: section.to })}
              >
                {t(`bot.nav.${section.key}`)}
              </Link>
            ))}
          </nav>
          <Outlet />
        </>
      )}
    </section>
  );
}
