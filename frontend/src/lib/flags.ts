/** Страны для флага в имени сервера (коды ISO 3166-1 alpha-2). Названия — на языке интерфейса, из Intl.DisplayNames. */
export const COUNTRY_CODES: readonly string[] = [
  "AT",
  "AM",
  "BE",
  "BG",
  "GB",
  "HU",
  "DE",
  "HK",
  "GE",
  "DK",
  "IL",
  "IN",
  "ES",
  "IT",
  "KZ",
  "CA",
  "CY",
  "LV",
  "LT",
  "LU",
  "MD",
  "NL",
  "NO",
  "AE",
  "PL",
  "PT",
  "RU",
  "RO",
  "RS",
  "SG",
  "SK",
  "US",
  "TR",
  "UA",
  "FI",
  "FR",
  "HR",
  "CZ",
  "CH",
  "SE",
  "EE",
  "JP",
];

const REGIONAL_INDICATOR_A = 0x1f1e6;

/** "DE" → "🇩🇪": две региональные буквы Unicode, которые система рисует флагом. */
export function flagEmoji(code: string): string {
  return [...code.toUpperCase()]
    .map((letter) => String.fromCodePoint(REGIONAL_INDICATOR_A + letter.charCodeAt(0) - 65))
    .join("");
}

// Флаг в начале имени: пара региональных букв и необязательный пробел после неё.
const LEADING_FLAG = /^([\u{1F1E6}-\u{1F1FF}]{2})\s*/u;

/** Имя сервера → флаг (если имя с него начинается) и остальное имя. */
export function splitFlag(name: string): { flag: string; rest: string } {
  const match = LEADING_FLAG.exec(name);
  return match?.[1] ? { flag: match[1], rest: name.slice(match[0].length) } : { flag: "", rest: name };
}

/** Флаг + имя → имя для панели. Без флага имя остаётся как есть. */
export function withFlag(flag: string, name: string): string {
  return flag ? `${flag} ${name}` : name;
}

/** Запас под флаг и пробел: бэкенд ограничивает имя 100 символами (флаг — 2 символа Unicode). */
export const NAME_MAX_WITH_FLAG = 96;

/** Страны для выбора: флаг и название на языке интерфейса, по алфавиту этого языка. */
export function countryOptions(language: string): { flag: string; name: string }[] {
  const names = new Intl.DisplayNames(language, { type: "region" });
  return COUNTRY_CODES.map((code) => ({ flag: flagEmoji(code), name: names.of(code) ?? code })).sort((a, b) =>
    a.name.localeCompare(b.name, language),
  );
}

/** "🇩🇪" → "DE"; не флаг — null. */
export function flagCode(flag: string): string | null {
  const letters = [...flag].map((char) => char.codePointAt(0)! - REGIONAL_INDICATOR_A);
  if (letters.length !== 2 || letters.some((value) => value < 0 || value > 25)) return null;
  return letters.map((value) => String.fromCharCode(65 + value)).join("");
}

/** Название страны по флагу на языке интерфейса; нет флага или код незнаком — null. */
export function flagCountryName(flag: string, language: string): string | null {
  const code = flagCode(flag);
  if (!code) return null;
  return new Intl.DisplayNames(language, { type: "region" }).of(code) ?? null;
}
