/*
 * Проверка и разбор текста новости в формате Telegram HTML: строгий белый список, отказ вместо правки.
 * Правила один в один как на сервере (src/modules/bot/telegram_html.py); общий набор примеров -
 * src/lib/telegramHtml.cases.json читают и pytest, и vitest, поэтому проверка и предпросмотр не разойдутся.
 * Текст никогда не правится молча: годится он или нет - вердикт, а не «очищенная» строка.
 */

export const MAX_VISIBLE = 4096; // видимый текст в единицах UTF-16 (эмодзи - две), как считает Telegram
export const MAX_SOURCE = 16384; // исходник вместе с разметкой

export type TelegramHtmlCode =
  | "telegram_html_tag"
  | "telegram_html_attr"
  | "telegram_html_entity"
  | "telegram_html_nesting"
  | "telegram_html_length"
  | "telegram_html_empty";

const TAG: TelegramHtmlCode = "telegram_html_tag";
const ATTR: TelegramHtmlCode = "telegram_html_attr";
const ENTITY: TelegramHtmlCode = "telegram_html_entity";
const NESTING: TelegramHtmlCode = "telegram_html_nesting";
const LENGTH: TelegramHtmlCode = "telegram_html_length";
const EMPTY: TelegramHtmlCode = "telegram_html_empty";

export const TELEGRAM_HTML_CODES: readonly TelegramHtmlCode[] = [TAG, ATTR, ENTITY, NESTING, LENGTH, EMPTY];

const PLAIN_TAGS = new Set(["b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "tg-spoiler"]);
const KNOWN_TAGS = new Set([...PLAIN_TAGS, "span", "a", "code", "pre", "blockquote"]);
const NAMED_ENTITIES: Record<string, string> = { lt: "<", gt: ">", amp: "&", quot: '"' };

const ENTITY_RE = /&(?:#([0-9]{1,7})|#[xX]([0-9A-Fa-f]{1,6})|([A-Za-z]{1,8}));/y;
// Пробел в разметке - явный набор, а не класс s: у JavaScript и Python он разный (U+FEFF, управляющие, U+0085).
const OPEN_TAG_RE =
  /<([A-Za-z][A-Za-z0-9-]*)((?:[ \t\n\r\f]+[^ \t\n\r\f=>/"'<]+(?:[ \t\n\r\f]*=[ \t\n\r\f]*(?:"[^"]*"|'[^']*'))?)*)[ \t\n\r\f]*>/y;
const CLOSE_TAG_RE = /<\/([A-Za-z][A-Za-z0-9-]*)[ \t\n\r\f]*>/y;
const ATTRIBUTE_RE = /([^ \t\n\r\f=>/"'<]+)(?:[ \t\n\r\f]*=[ \t\n\r\f]*(?:"([^"]*)"|'([^']*)'))?/g;
const TAG_NAME_RE = /<([A-Za-z][A-Za-z0-9-]*)(?=[ \t\n\r\f/>])/y;
const LANGUAGE_RE = /^language-[A-Za-z0-9_+#.-]{1,32}$/;
const HTTP_RE = /^https?:\/\/[^ \t\n\r\f]/i;

/** Узел предпросмотра: строка - текст, объект - тег из белого списка (псевдонимы сведены к одному имени). */
export type TelegramTag = "b" | "i" | "u" | "s" | "spoiler" | "a" | "code" | "pre" | "blockquote";
export interface TelegramElement {
  tag: TelegramTag;
  children: TelegramNode[];
  /** Только у `a`: адрес. В предпросмотре по нему не переходят, он нужен лишь для полноты разбора. */
  href?: string;
  /** Только у `blockquote expandable`. */
  expandable?: boolean;
}
export type TelegramNode = string | TelegramElement;

export type TelegramHtmlResult = {
  ok: boolean;
  /** Одно из telegram_html_*; при ok = true не задано. */
  code?: TelegramHtmlCode;
  /** Индекс символа исходника, на котором разбор остановился. */
  position?: number;
  /** Видимая длина текста в единицах UTF-16 (при отказе - сколько успело набраться до ошибки). */
  visible_length: number;
};

class TelegramHtmlError extends Error {
  constructor(
    readonly code: TelegramHtmlCode,
    readonly position: number,
  ) {
    super(`${code} at ${position}`); // ни кусочка текста: сообщение может попасть в логи
  }
}

function forbidden(char: string): boolean {
  const code = char.codePointAt(0) ?? 0;
  if (char === "\n" || char === "\t") return false;
  return (
    code < 0x20 ||
    (code >= 0x7f && code <= 0x9f) ||
    (code >= 0x202a && code <= 0x202e) ||
    (code >= 0x2066 && code <= 0x2069)
  );
}

/** Подставить сущности (только &lt; &gt; &amp; &quot; и числовые); всё прочее с «&» - ошибка. */
function decode(text: string, offset: number): string {
  let result = "";
  let index = 0;
  while (index < text.length) {
    const char = String.fromCodePoint(text.codePointAt(index) ?? 0);
    if (char !== "&") {
      if (forbidden(char)) throw new TelegramHtmlError(ENTITY, offset + index);
      result += char;
      index += char.length;
      continue;
    }
    ENTITY_RE.lastIndex = index;
    const match = ENTITY_RE.exec(text);
    if (match === null) throw new TelegramHtmlError(ENTITY, offset + index);
    const [whole, decimal, hexadecimal, named] = match;
    if (named !== undefined) {
      const value = NAMED_ENTITIES[named];
      if (value === undefined) throw new TelegramHtmlError(ENTITY, offset + index);
      result += value;
    } else {
      const number = decimal !== undefined ? Number(decimal) : parseInt(hexadecimal ?? "", 16);
      if (number === 0 || number > 0x10ffff || (number >= 0xd800 && number <= 0xdfff)) {
        throw new TelegramHtmlError(ENTITY, offset + index);
      }
      const decoded = String.fromCodePoint(number);
      if (forbidden(decoded)) throw new TelegramHtmlError(ENTITY, offset + index); // спрятан в сущности
      result += decoded;
    }
    index += whole.length;
  }
  return result;
}

function readAttributes(raw: string, position: number): Map<string, string | null> {
  const found = new Map<string, string | null>();
  for (const match of raw.matchAll(ATTRIBUTE_RE)) {
    const name = (match[1] ?? "").toLowerCase();
    const value = match[2] ?? match[3];
    if (found.has(name)) throw new TelegramHtmlError(ATTR, position);
    found.set(name, value === undefined ? null : decode(value, position));
  }
  return found;
}

/** Допустимые атрибуты каждого тега: любое другое - отказ. */
function checkAttributes(
  name: string,
  attrs: Map<string, string | null>,
  parent: string | undefined,
  position: number,
): void {
  const fail = () => {
    throw new TelegramHtmlError(ATTR, position);
  };
  if (PLAIN_TAGS.has(name) || name === "pre") {
    if (attrs.size > 0) fail();
  } else if (name === "span") {
    if (attrs.size !== 1 || attrs.get("class") !== "tg-spoiler") fail();
  } else if (name === "a") {
    const href = attrs.get("href");
    if (attrs.size !== 1 || href === undefined || href === null) return fail();
    if (!HTTP_RE.test(href)) fail(); // только http/https, без javascript:, data:, tg://
  } else if (name === "code") {
    if (attrs.size > 0) {
      const language = attrs.get("class");
      if (parent !== "pre" || attrs.size !== 1 || language == null || !LANGUAGE_RE.test(language)) fail();
    }
  } else if (name === "blockquote") {
    for (const key of attrs.keys()) if (key !== "expandable") fail();
    const expandable = attrs.get("expandable");
    if (expandable !== undefined && expandable !== null && expandable !== "" && expandable !== "expandable")
      fail();
  }
}

function checkOpening(name: string, stack: string[], position: number): void {
  const parent = stack.at(-1);
  if (parent === "code" || (parent === "pre" && name !== "code")) {
    throw new TelegramHtmlError(NESTING, position); // внутри code и pre разметки нет (в pre допустим один code)
  }
  if (name === "a" && stack.includes("a")) throw new TelegramHtmlError(NESTING, position);
  if (name === "blockquote" && stack.includes("blockquote")) throw new TelegramHtmlError(NESTING, position);
}

const ALIASES: Record<string, TelegramTag> = {
  b: "b",
  strong: "b",
  i: "i",
  em: "i",
  u: "u",
  ins: "u",
  s: "s",
  strike: "s",
  del: "s",
  "tg-spoiler": "spoiler",
  span: "spoiler",
  a: "a",
  code: "code",
  pre: "pre",
  blockquote: "blockquote",
};

interface Scan {
  nodes: TelegramNode[];
  visible: number;
}

/** Один проход по тексту: строит дерево и считает видимую длину. Не годится - бросает TelegramHtmlError. */
function scan(text: string, progress: { visible: number }): Scan {
  const root: TelegramNode[] = [];
  const stack: string[] = [];
  const open: TelegramElement[] = [];
  const children = () => open.at(-1)?.children ?? root;
  let hasContent = false;
  let index = 0;
  const size = text.length;

  if (size > MAX_SOURCE) {
    progress.visible = size;
    throw new TelegramHtmlError(LENGTH, MAX_SOURCE);
  }

  while (index < size) {
    if (text[index] === "<") {
      CLOSE_TAG_RE.lastIndex = index;
      const closing = CLOSE_TAG_RE.exec(text);
      if (closing !== null) {
        const name = (closing[1] ?? "").toLowerCase();
        if (!KNOWN_TAGS.has(name)) throw new TelegramHtmlError(TAG, index);
        if (stack.length === 0 || stack.at(-1) !== name) throw new TelegramHtmlError(NESTING, index);
        stack.pop();
        open.pop();
        index += closing[0].length;
        continue;
      }
      OPEN_TAG_RE.lastIndex = index;
      const opening = OPEN_TAG_RE.exec(text);
      if (opening === null) {
        TAG_NAME_RE.lastIndex = index;
        const named = TAG_NAME_RE.exec(text);
        if (named === null) throw new TelegramHtmlError(ENTITY, index); // «<» не начало тега: пишется как &lt;
        // Тег есть, но атрибуты не по правилам (без кавычек, «/>»): чужой тег - TAG, свой - ATTR.
        throw new TelegramHtmlError(KNOWN_TAGS.has((named[1] ?? "").toLowerCase()) ? ATTR : TAG, index);
      }
      const name = (opening[1] ?? "").toLowerCase();
      if (!KNOWN_TAGS.has(name)) throw new TelegramHtmlError(TAG, index);
      const attrs = readAttributes(opening[2] ?? "", index);
      checkAttributes(name, attrs, stack.at(-1), index);
      checkOpening(name, stack, index);
      const element: TelegramElement = { tag: ALIASES[name] as TelegramTag, children: [] };
      if (name === "a") element.href = attrs.get("href") ?? "";
      if (name === "blockquote" && attrs.has("expandable")) element.expandable = true;
      children().push(element);
      stack.push(name);
      open.push(element);
      index += opening[0].length;
      continue;
    }
    const end = text.indexOf("<", index);
    const chunk = text.slice(index, end === -1 ? size : end);
    const decoded = decode(chunk, index);
    progress.visible += decoded.length; // длина строки JS - это и есть единицы UTF-16
    hasContent = hasContent || /[^ \t\n\r\f]/.test(decoded);
    children().push(decoded);
    index += chunk.length;
  }
  if (stack.length > 0) throw new TelegramHtmlError(NESTING, size);
  if (!hasContent) throw new TelegramHtmlError(EMPTY, 0);
  if (progress.visible > MAX_VISIBLE) throw new TelegramHtmlError(LENGTH, 0);
  return { nodes: root, visible: progress.visible };
}

/** Вердикт по тексту. Это только вердикт: «исправленного» текста нет, отправляется то, что написал админ. */
export function validateTelegramHtml(input: string): TelegramHtmlResult {
  const progress = { visible: 0 };
  try {
    const { visible } = scan(input, progress);
    return { ok: true, visible_length: visible };
  } catch (exc) {
    if (exc instanceof TelegramHtmlError) {
      return { ok: false, code: exc.code, position: exc.position, visible_length: progress.visible };
    }
    throw exc;
  }
}

/**
 * Узлы для предпросмотра. Текст, не прошедший проверку, не разбирается: он целиком становится одним текстовым
 * узлом, чтобы админ видел ровно то, что написал (теги из такого текста в предпросмотре не появляются).
 */
export function parseTelegramHtml(input: string): TelegramNode[] {
  try {
    return scan(input, { visible: 0 }).nodes;
  } catch (exc) {
    if (exc instanceof TelegramHtmlError) return input === "" ? [] : [input];
    throw exc;
  }
}

/** Видимый текст узлов, без разметки. */
export function plainText(nodes: TelegramNode[]): string {
  return nodes.map((node) => (typeof node === "string" ? node : plainText(node.children))).join("");
}
