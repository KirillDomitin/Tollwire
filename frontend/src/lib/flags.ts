/** Страны для флага в имени сервера: код ISO 3166-1 alpha-2 и название. Флаг-эмодзи строится из кода. */
export const COUNTRIES: ReadonlyArray<readonly [code: string, name: string]> = [
  ["AT", "Австрия"],
  ["AM", "Армения"],
  ["BE", "Бельгия"],
  ["BG", "Болгария"],
  ["GB", "Великобритания"],
  ["HU", "Венгрия"],
  ["DE", "Германия"],
  ["HK", "Гонконг"],
  ["GE", "Грузия"],
  ["DK", "Дания"],
  ["IL", "Израиль"],
  ["IN", "Индия"],
  ["ES", "Испания"],
  ["IT", "Италия"],
  ["KZ", "Казахстан"],
  ["CA", "Канада"],
  ["CY", "Кипр"],
  ["LV", "Латвия"],
  ["LT", "Литва"],
  ["LU", "Люксембург"],
  ["MD", "Молдова"],
  ["NL", "Нидерланды"],
  ["NO", "Норвегия"],
  ["AE", "ОАЭ"],
  ["PL", "Польша"],
  ["PT", "Португалия"],
  ["RU", "Россия"],
  ["RO", "Румыния"],
  ["RS", "Сербия"],
  ["SG", "Сингапур"],
  ["SK", "Словакия"],
  ["US", "США"],
  ["TR", "Турция"],
  ["UA", "Украина"],
  ["FI", "Финляндия"],
  ["FR", "Франция"],
  ["HR", "Хорватия"],
  ["CZ", "Чехия"],
  ["CH", "Швейцария"],
  ["SE", "Швеция"],
  ["EE", "Эстония"],
  ["JP", "Япония"],
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
