import { parseTelegramHtml, validateTelegramHtml } from "./telegramHtml";
import rawCases from "./telegramHtml.cases.json";

/*
 * ДОПУЩЕНИЯ о коде, которого ещё нет (этап 25.4, красные тесты):
 * - `src/lib/telegramHtml.ts` экспортирует:
 *     validateTelegramHtml(input: string): { ok: boolean; code?: string; visible_length: number }
 *       code - один из telegram_html_tag | attr | entity | nesting | length | empty (как `type` в 422 бэкенда);
 *       при ok = true code не задан; visible_length - видимая длина в единицах UTF-16 (эмодзи = 2).
 *     parseTelegramHtml(input: string): узлы для предпросмотра (строка - текст; объект с массивом `children` - тег).
 *       Форма узла в тестах не фиксируется: проверяется только видимый текст (обход по `children`).
 * - `src/lib/telegramHtml.cases.json` - общий набор примеров бэкенда и фронтенда:
 *     массив { name, input, ok, code?, visible_length? }. Файл создаёт агент-бэкендер; если его нет, лежит
 *     минимальная версия этого агента тестов (её можно заменить целиком, тесты читают только формат).
 *     Для ok = false без `code` проверяется только то, что отказ - один из telegram_html_*.
 */

interface Case {
  name: string;
  input: string;
  ok: boolean;
  code?: string;
  visible_length?: number;
}
const CASES = rawCases as unknown as Case[];

const CODES = [
  "telegram_html_tag",
  "telegram_html_attr",
  "telegram_html_entity",
  "telegram_html_nesting",
  "telegram_html_length",
  "telegram_html_empty",
];

/** Видимый текст узлов, какой бы формы они ни были: строки и содержимое `children`. */
function plain(nodes: unknown): string {
  if (typeof nodes === "string") return nodes;
  if (Array.isArray(nodes)) return nodes.map(plain).join("");
  if (nodes && typeof nodes === "object" && "children" in nodes)
    return plain((nodes as { children: unknown }).children);
  return "";
}

describe("shared cases file", () => {
  it("is a list of cases with a name, an input and a verdict", () => {
    expect(CASES.length).toBeGreaterThan(10);
    for (const item of CASES) {
      expect(typeof item.name).toBe("string");
      expect(typeof item.input).toBe("string");
      expect(typeof item.ok).toBe("boolean");
    }
    expect(new Set(CASES.map((item) => item.name)).size).toBe(CASES.length); // имя однозначно называет случай
  });

  it("covers every tag, the attribute and entity rules, the length limit and the dangerous characters", () => {
    const inputs = CASES.map((item) => item.input).join("\n");
    for (const marker of [
      "<b>",
      "<strong>",
      "<i>",
      "<em>",
      "<u>",
      "<ins>",
      "<s>",
      "<strike>",
      "<del>",
      "<tg-spoiler>",
      'class="tg-spoiler"',
      "<a href",
      "<code>",
      "<pre>",
      "language-",
      "<blockquote",
      "expandable",
      "javascript:",
      "&nbsp;",
      "<tg-emoji",
      "<tg-time",
      "‮",
      "‍",
    ]) {
      expect(inputs, `no case with ${JSON.stringify(marker)}`).toContain(marker);
    }
    const codes = new Set(CASES.map((item) => item.code));
    for (const code of CODES) expect(codes, `no case with ${code}`).toContain(code);
    expect(CASES.some((item) => item.visible_length === 4096 && item.ok)).toBe(true);
    expect(CASES.some((item) => item.visible_length === 4097 && !item.ok)).toBe(true);
  });
});

describe("validateTelegramHtml on the shared cases", () => {
  it.each(CASES.map((item) => [item.name, item] as const))("%s", (_name, item) => {
    const result = validateTelegramHtml(item.input);

    expect(result.ok).toBe(item.ok);
    if (item.ok) {
      expect(result.code).toBeUndefined();
    } else if (item.code) {
      expect(result.code).toBe(item.code);
    } else {
      expect(result.code).toMatch(/^telegram_html_(tag|attr|entity|nesting|length|empty)$/);
    }
    if (item.visible_length !== undefined) expect(result.visible_length).toBe(item.visible_length);
  });
});

describe("parseTelegramHtml (the preview) on the shared cases", () => {
  it.each(CASES.filter((item) => item.ok).map((item) => [item.name, item] as const))(
    "%s: the preview text is exactly the visible text",
    (_name, item) => {
      const nodes = parseTelegramHtml(item.input);

      expect(nodes).toBeDefined();
      if (item.visible_length !== undefined) expect(plain(nodes).length).toBe(item.visible_length);
    },
  );

  it("keeps entities decoded in the text, like Telegram does", () => {
    expect(plain(parseTelegramHtml("a &amp; b &lt;c&gt;"))).toBe("a & b <c>");
  });
});

describe("validateTelegramHtml: rules that must hold whatever the cases file says", () => {
  it("counts an emoji as two UTF-16 units and the tags as nothing", () => {
    expect(validateTelegramHtml("😀").visible_length).toBe(2);
    expect(validateTelegramHtml("<b>ab</b>").visible_length).toBe(2);
    expect(validateTelegramHtml("a &amp; b").visible_length).toBe(5);
  });

  it("accepts 4096 units and rejects 4097, counting emoji twice", () => {
    expect(validateTelegramHtml("a".repeat(4096))).toMatchObject({ ok: true, visible_length: 4096 });
    expect(validateTelegramHtml("a".repeat(4097))).toMatchObject({ ok: false, code: "telegram_html_length" });
    expect(validateTelegramHtml("😀".repeat(2048)).ok).toBe(true);
    expect(validateTelegramHtml("😀".repeat(2049))).toMatchObject({
      ok: false,
      code: "telegram_html_length",
    });
  });

  it("rejects an empty text and a text of empty tags", () => {
    expect(validateTelegramHtml("")).toMatchObject({ ok: false, code: "telegram_html_empty" });
    expect(validateTelegramHtml("<b></b>")).toMatchObject({ ok: false, code: "telegram_html_empty" });
  });

  it("rejects script, img with onerror and links that are not http(s)", () => {
    expect(validateTelegramHtml("<script>alert(1)</script>").ok).toBe(false);
    expect(validateTelegramHtml("<img src=x onerror=alert(1)>")).toMatchObject({
      ok: false,
      code: "telegram_html_tag",
    });
    expect(validateTelegramHtml('<a href="javascript:alert(1)">x</a>').ok).toBe(false);
    expect(validateTelegramHtml('<a href="https://example.com">x</a>').ok).toBe(true);
  });

  it("rejects the right-to-left override but lets the zero-width joiner of emoji through", () => {
    expect(validateTelegramHtml("a‮b").ok).toBe(false);
    expect(validateTelegramHtml("👨‍👩‍👧").ok).toBe(true);
  });

  it("is a verdict only: it never returns a cleaned-up text", () => {
    const result = validateTelegramHtml("<b>x</b><script>y</script>") as Record<string, unknown>;

    expect(result.ok).toBe(false);
    for (const field of ["html", "text", "sanitized", "output"]) expect(result[field]).toBeUndefined();
  });
});
