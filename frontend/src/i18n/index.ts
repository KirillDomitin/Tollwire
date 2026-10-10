import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";

import en from "../locales/en";
import ru from "../locales/ru";

export const LANGUAGES = ["ru", "en"] as const;
export type Language = (typeof LANGUAGES)[number];

/** Выбор языка — удобство конкретного браузера (как и тема), поэтому localStorage. */
const STORAGE_KEY = "vpn-panel-lang";

/** Язык по браузеру: русский → русский, любой другой → английский. Явный выбор в панели важнее. */
void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: { ru: { translation: ru }, en: { translation: en } },
    supportedLngs: LANGUAGES,
    nonExplicitSupportedLngs: true,
    load: "languageOnly",
    fallbackLng: "en",
    detection: {
      order: ["localStorage", "navigator"],
      lookupLocalStorage: STORAGE_KEY,
      caches: ["localStorage"],
    },
    interpolation: { escapeValue: false }, // React сам экранирует вывод
  });

// <html lang> следует за языком: от него зависят переносы, экранные дикторы и подбор шрифта.
function syncDocumentLanguage(language: string): void {
  document.documentElement.lang = language;
}
syncDocumentLanguage(i18n.resolvedLanguage ?? i18n.language);
i18n.on("languageChanged", syncDocumentLanguage);

export default i18n;
