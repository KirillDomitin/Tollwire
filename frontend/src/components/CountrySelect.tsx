import { useId } from "react";
import { useTranslation } from "react-i18next";

import { countryOptions } from "../lib/flags";
import styles from "./TextField.module.css";

/** Выбор страны для флага в имени сервера. Значение — сам эмодзи ("" — без флага). */
export function CountrySelect({ name, defaultValue = "" }: { name: string; defaultValue?: string }) {
  const { t, i18n } = useTranslation();
  const id = useId();
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {t("common.countrySelect.label")}
      </label>
      <select id={id} name={name} className={styles.input} defaultValue={defaultValue}>
        <option value="">{t("common.countrySelect.none")}</option>
        {countryOptions(i18n.resolvedLanguage ?? "en").map(({ flag, name: country }) => (
          <option key={flag} value={flag}>
            {flag} {country}
          </option>
        ))}
      </select>
    </div>
  );
}
