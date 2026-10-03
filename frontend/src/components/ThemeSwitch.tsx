import { useState } from "react";

import { applyTheme, loadTheme, type ThemeChoice } from "../lib/theme";
import styles from "./ThemeSwitch.module.css";

const OPTIONS: { value: ThemeChoice; label: string }[] = [
  { value: "system", label: "Авто" },
  { value: "light", label: "Светлая" },
  { value: "dark", label: "Тёмная" },
];

export function ThemeSwitch() {
  const [choice, setChoice] = useState<ThemeChoice>(loadTheme);
  return (
    <fieldset className={styles.switch}>
      <legend className={styles.legend}>Тема</legend>
      <div className={styles.options}>
        {OPTIONS.map((option) => (
          <label key={option.value} className={styles.option}>
            <input
              title={option.value === "system" ? "Как в системе" : undefined}
              type="radio"
              name="theme"
              value={option.value}
              checked={choice === option.value}
              onChange={() => {
                setChoice(option.value);
                applyTheme(option.value);
              }}
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
