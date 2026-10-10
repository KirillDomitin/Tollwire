import "i18next";

import type ru from "../locales/ru";

// Типы ключей берутся из русского словаря: t("shell.nav.servers") с опечаткой не пройдёт tsc.
// Совпадение набора ключей ru и en проверяет тест (i18n.test.ts).
declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: "translation";
    resources: { translation: typeof ru };
  }
}
