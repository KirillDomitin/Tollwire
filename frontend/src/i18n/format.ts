import i18n from "./index";

/** Текущий язык интерфейса — для Intl-форматтеров (даты, числа, склонение). */
export function currentLanguage(): string {
  return i18n.resolvedLanguage ?? i18n.language ?? "en";
}

/** Intl-объекты дорого создавать на каждую ячейку таблицы: один на язык и набор параметров. */
export function memoByLanguage<T>(create: (language: string) => T): () => T {
  const cache = new Map<string, T>();
  return () => {
    const language = currentLanguage();
    let value = cache.get(language);
    if (value === undefined) {
      value = create(language);
      cache.set(language, value);
    }
    return value;
  };
}
