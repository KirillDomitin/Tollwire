import { useTranslation } from "react-i18next";

import { LANGUAGES } from "../i18n";
import styles from "./ThemeSwitch.module.css"; // тот же сегментный переключатель, что и у темы

export function LanguageSwitch() {
  const { t, i18n } = useTranslation();
  const current = i18n.resolvedLanguage;
  return (
    <fieldset className={styles.switch}>
      <legend className={styles.legend}>{t("shell.language.legend")}</legend>
      <div className={styles.options}>
        {LANGUAGES.map((language) => (
          <label key={language} className={styles.option}>
            <input
              title={t(`shell.language.${language}Title`)}
              type="radio"
              name="language"
              value={language}
              checked={current === language}
              onChange={() => void i18n.changeLanguage(language)}
            />
            <span>{t(`shell.language.${language}`)}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
