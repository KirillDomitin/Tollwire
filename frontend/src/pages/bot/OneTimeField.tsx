import { useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../components/Button";
import styles from "./Bot.module.css";

/**
 * Секрет, который показывают один раз (ключ бота, ссылка-приглашение): поле только для чтения и «Скопировать».
 * Не вышло скопировать (во встроенных браузерах буфера может не быть) - поле выделяется и просят скопировать вручную.
 */
export function OneTimeField({
  label,
  value,
  multiline = false,
  copyLabel,
}: {
  label: string;
  value: string;
  multiline?: boolean;
  copyLabel?: string;
}) {
  const { t } = useTranslation();
  const id = useId();
  const field = useRef<HTMLInputElement | HTMLTextAreaElement>(null);
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setFailed(false);
    } catch {
      setCopied(false);
      setFailed(true);
      field.current?.select();
    }
  }

  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {label}
      </label>
      {multiline ? (
        <textarea
          id={id}
          ref={(node) => {
            field.current = node;
          }}
          readOnly
          value={value}
          rows={Math.min(value.split("\n").length, 8)}
          className={`${styles.secret} ${styles.textarea}`}
          autoComplete="off"
          spellCheck={false}
          onFocus={(event) => event.currentTarget.select()}
        />
      ) : (
        <input
          id={id}
          ref={(node) => {
            field.current = node;
          }}
          readOnly
          value={value}
          className={styles.secret}
          autoComplete="off"
          spellCheck={false}
          onFocus={(event) => event.currentTarget.select()}
        />
      )}
      <div className={styles.row}>
        <Button variant="ghost" onClick={() => void copy()}>
          {copied ? t("bot.copied") : (copyLabel ?? t("bot.copy"))}
        </Button>
        {failed && (
          <p role="status" className={styles.hint}>
            {t("bot.copyFailed")}
          </p>
        )}
      </div>
    </div>
  );
}
