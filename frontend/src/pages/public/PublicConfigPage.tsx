import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router";

import {
  fetchPublicConfig,
  PublicConfigError,
  type PublicConfig,
  type PublicConfigFailure,
} from "../../api/public";
import { BrandMark } from "../../components/BrandMark";
import { Button } from "../../components/Button";
import { LanguageSwitch } from "../../components/LanguageSwitch";
import { QrCode, QrSeries } from "../../components/QrSeries";
import { currentLanguage } from "../../i18n/format";
import styles from "./PublicConfigPage.module.css";

type View =
  | { kind: "loading" }
  | { kind: "ready"; page: PublicConfig }
  | { kind: "error"; failure: PublicConfigFailure }
  | { kind: "hidden" };

const PLATFORMS = ["android", "ios", "windows", "macos"] as const;

function formatDate(iso: string): string {
  return new Intl.DateTimeFormat(currentLanguage(), {
    day: "numeric",
    month: "long",
    year: "numeric",
  }).format(new Date(iso));
}

/**
 * Личная страница конфига: адрес `/c#<токен>`, открывается без входа. Токен читается из фрагмента, уходит только в
 * теле POST и нигде не сохраняется; конфиг живёт лишь в состоянии страницы и стирается, когда страницу прячут.
 */
export function PublicConfigPage() {
  const { t } = useTranslation();
  const token = useLocation().hash.replace(/^#/, "");
  const [view, setView] = useState<View>(token ? { kind: "loading" } : { kind: "error", failure: "invalid" });

  useEffect(() => {
    document.title = t("public.documentTitle"); // без имени и токена: заголовок попадает в историю и закладки
  }, [t]);

  useEffect(() => {
    if (!token) return; // начальное состояние уже «ссылка недействительна»
    const controller = new AbortController();
    fetchPublicConfig(token, controller.signal).then(
      (page) => setView({ kind: "ready", page }),
      (exc: unknown) => {
        if (controller.signal.aborted) return;
        setView({ kind: "error", failure: exc instanceof PublicConfigError ? exc.failure : "failed" });
      },
    );
    return () => controller.abort();
  }, [token]);

  // Страницу уводят со вкладки (закрыли, ушли, кэш «назад»): конфиг с экрана и из памяти убираем.
  useEffect(() => {
    const hide = () => setView({ kind: "hidden" });
    window.addEventListener("pagehide", hide);
    return () => window.removeEventListener("pagehide", hide);
  }, []);

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <BrandMark />
        <LanguageSwitch />
      </header>
      <main className={styles.main}>
        <h1 className={styles.title}>{t("public.title")}</h1>
        {view.kind === "loading" && <p aria-busy="true">{t("public.loading")}</p>}
        {view.kind === "error" && <Problem scope={view.failure} />}
        {view.kind === "hidden" && <Problem scope="hidden" />}
        {view.kind === "ready" && <Config page={view.page} />}
      </main>
    </div>
  );
}

const PROBLEM_TEXT = {
  invalid: { title: "public.errors.invalid.title", hint: "public.errors.invalid.hint" },
  tooMany: { title: "public.errors.tooMany.title", hint: "public.errors.tooMany.hint" },
  network: { title: "public.errors.network.title", hint: "public.errors.network.hint" },
  failed: { title: "public.errors.failed.title", hint: "public.errors.failed.hint" },
  hidden: { title: "public.hidden.title", hint: "public.hidden.hint" },
} as const;

function Problem({ scope }: { scope: keyof typeof PROBLEM_TEXT }) {
  const { t } = useTranslation();
  return (
    <div role="alert" className={styles.problem}>
      <h2>{t(PROBLEM_TEXT[scope].title)}</h2>
      <p>{t(PROBLEM_TEXT[scope].hint)}</p>
    </div>
  );
}

function Config({ page }: { page: PublicConfig }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const shareLink = page.share_link ?? null;
  const shareQr = page.share_qr ?? null;
  const keyText = shareLink ?? page.config;

  async function copy() {
    await navigator.clipboard.writeText(keyText);
    setCopied(true);
  }

  function download() {
    const url = URL.createObjectURL(new Blob([page.config], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${page.file_name}.conf`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <dl className={styles.facts}>
        <div>
          <dt>{t("public.name")}</dt>
          <dd>
            <strong>{page.name}</strong>
          </dd>
        </div>
        <div>
          <dt>{t("public.validUntil")}</dt>
          <dd>{formatDate(page.valid_until)}</dd>
        </div>
        <div>
          <dt>{t("public.server")}</dt>
          <dd>{page.server_name}</dd>
        </div>
      </dl>

      <section className={styles.section} aria-labelledby="public-key">
        <h2 id="public-key">{t("public.key.title")}</h2>
        <label className={styles.field}>
          <span>{shareLink ? t("public.key.label") : t("public.key.labelConfig")}</span>
          {shareLink ? (
            <input readOnly value={shareLink} className={styles.input} />
          ) : (
            <textarea readOnly value={page.config} rows={8} className={styles.input} />
          )}
        </label>
        <div className={styles.actions}>
          <Button variant="quiet" onClick={() => void copy()}>
            {copied ? t("public.key.copied") : shareLink ? t("public.key.copy") : t("public.key.copyConfig")}
          </Button>
          <Button variant="quiet" onClick={download}>
            {t("public.download")}
          </Button>
        </div>
        <p className={styles.hint}>{t("public.downloadHint")}</p>
      </section>

      <section className={styles.section} aria-labelledby="public-qr">
        <h2 id="public-qr">{t("public.qr.title")}</h2>
        <p className={styles.hint}>{t("public.qr.hint")}</p>
        {shareQr && shareQr.length > 0 ? <QrSeries codes={shareQr} /> : <QrCode text={keyText} />}
      </section>

      <section className={styles.section} aria-labelledby="public-how">
        <h2 id="public-how">{t("public.how.title")}</h2>
        {PLATFORMS.map((platform) => (
          <details key={platform} className={styles.details}>
            <summary>{t(`public.how.${platform}.summary`)}</summary>
            <p>{t(`public.how.${platform}.steps`)}</p>
          </details>
        ))}
      </section>

      <p className={styles.warning}>{t("public.warning")}</p>
    </>
  );
}
