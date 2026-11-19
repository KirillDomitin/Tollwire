import { useId } from "react";
import { useTranslation } from "react-i18next";

import styles from "../../components/TextField.module.css";
import { cityName, serverCities } from "./serverCities";

export function CitySelect({ value, onChange }: { value?: string; onChange?: (value: string) => void }) {
  const { t, i18n } = useTranslation();
  const id = useId();
  const language = i18n.resolvedLanguage ?? "en";
  const countries = new Intl.DisplayNames(language, { type: "region" });
  const options = Array.from(serverCities.values(), (city) => {
    const country = city.id.slice(0, 2).toUpperCase();
    return { id: city.id, name: `${cityName(city, language)}, ${countries.of(country) ?? country}` };
  }).sort((a, b) => a.name.localeCompare(b.name, language));

  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {t("servers.city.label")}
      </label>
      <select
        id={id}
        name="location_city_id"
        className={styles.input}
        value={value}
        onChange={onChange && ((event) => onChange(event.currentTarget.value))}
      >
        <option value="">{t("servers.city.none")}</option>
        {value && !serverCities.has(value) && <option value={value}>{t("servers.city.unknown")}</option>}
        {options.map((option) => (
          <option key={option.id} value={option.id}>
            {option.name}
          </option>
        ))}
      </select>
    </div>
  );
}
