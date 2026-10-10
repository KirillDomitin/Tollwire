import { useId } from "react";

import { COUNTRIES, flagEmoji } from "../lib/flags";
import styles from "./TextField.module.css";

/** Выбор страны для флага в имени сервера. Значение — сам эмодзи ("" — без флага). */
export function CountrySelect({ name, defaultValue = "" }: { name: string; defaultValue?: string }) {
  const id = useId();
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        Страна (необязательно)
      </label>
      <select id={id} name={name} className={styles.input} defaultValue={defaultValue}>
        <option value="">Без флага</option>
        {COUNTRIES.map(([code, country]) => (
          <option key={code} value={flagEmoji(code)}>
            {flagEmoji(code)} {country}
          </option>
        ))}
      </select>
    </div>
  );
}
