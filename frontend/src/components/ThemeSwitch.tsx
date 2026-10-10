import { useState } from "react";
import { useTranslation } from "react-i18next";

import { applyTheme, loadTheme, type ThemeChoice } from "../lib/theme";
import styles from "./ThemeSwitch.module.css";

const OPTIONS: ThemeChoice[] = ["system", "light", "dark"];

export function ThemeSwitch() {
  const { t } = useTranslation();
  const [choice, setChoice] = useState<ThemeChoice>(loadTheme);
  return (
    <fieldset className={styles.switch}>
      <legend className={styles.legend}>{t("shell.theme.legend")}</legend>
      <div className={styles.options}>
        {OPTIONS.map((value) => (
          <label key={value} className={styles.option}>
            <input
              title={value === "system" ? t("shell.theme.systemTitle") : undefined}
              type="radio"
              name="theme"
              value={value}
              checked={choice === value}
              onChange={() => {
                setChoice(value);
                applyTheme(value);
              }}
            />
            <span>{t(`shell.theme.${value}`)}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
