import i18n, { LANGUAGES } from "./index";
import en from "../locales/en";
import ru from "../locales/ru";

/** Плюральные формы у языков разные (ru: one/few/many/other, en: one/other) — сравниваем «корни» ключей. */
const PLURAL_SUFFIX = /_(zero|one|two|few|many|other)$/;

function keysOf(value: unknown, prefix = ""): string[] {
  if (typeof value !== "object" || value === null) return [prefix];
  return Object.entries(value).flatMap(([key, child]) => keysOf(child, prefix ? `${prefix}.${key}` : key));
}

const rootKeys = (dictionary: unknown) =>
  [...new Set(keysOf(dictionary).map((key) => key.replace(PLURAL_SUFFIX, "")))].sort();

describe("translations", () => {
  it("have the same keys in every language", () => {
    expect(rootKeys(en)).toEqual(rootKeys(ru));
  });

  it("have the public page dictionary (public.*) in both languages", () => {
    // Страница конфига (25.3) открывается без входа: её словарь не может зависеть от строк панели.
    const publicKeys = (dictionary: unknown) =>
      keysOf((dictionary as Record<string, unknown>).public).filter((key) => key !== "undefined");
    expect(publicKeys(ru).length).toBeGreaterThan(10);
    expect(rootKeys({ public: (en as Record<string, unknown>).public })).toEqual(
      rootKeys({ public: (ru as Record<string, unknown>).public }),
    );
  });

  it("tell the person of the public page to write /config to the bot", () => {
    for (const dictionary of [ru, en] as Record<string, Record<string, unknown>>[]) {
      const text = JSON.stringify(dictionary.public);
      expect(text).toContain("/config");
    }
  });

  it("have no empty strings", () => {
    for (const dictionary of [ru, en]) {
      const empty = keysOf(dictionary).filter((key) => {
        const value = key
          .split(".")
          .reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], dictionary);
        return typeof value === "string" && value.trim() === "";
      });
      expect(empty).toEqual([]);
    }
  });

  it("use the same {{placeholders}} in every language", () => {
    const placeholders = (text: string) =>
      [...text.matchAll(/{{\s*(\w+)\s*}}/g)].map((match) => match[1]).sort();
    const flat = (dictionary: unknown) =>
      Object.fromEntries(
        keysOf(dictionary).map((key) => [
          key,
          key
            .split(".")
            .reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], dictionary) as string,
        ]),
      );
    const russian = flat(ru);
    const english = flat(en);
    for (const [key, text] of Object.entries(russian)) {
      const counterpart = english[key];
      if (counterpart === undefined) continue; // плюральные формы, которых нет в другом языке
      expect({ key, placeholders: placeholders(counterpart) }).toEqual({
        key,
        placeholders: placeholders(text),
      });
    }
  });
});

describe("language selection", () => {
  afterEach(async () => {
    await i18n.changeLanguage("ru");
  });

  it("supports Russian and English", () => {
    expect([...LANGUAGES]).toEqual(["ru", "en"]);
  });

  it("switches the translation and the document language", async () => {
    await i18n.changeLanguage("en");
    expect(i18n.t("shell.nav.servers")).toBe("Servers");
    expect(document.documentElement.lang).toBe("en");

    await i18n.changeLanguage("ru");
    expect(i18n.t("shell.nav.servers")).toBe("Серверы");
    expect(document.documentElement.lang).toBe("ru");
  });

  it("falls back to English for a language it does not know", async () => {
    await i18n.changeLanguage("de");
    expect(i18n.t("shell.nav.servers")).toBe("Servers");
  });

  it("formats numbers and units in the current language", async () => {
    const { formatBytes, formatPercent } = await import("../lib/units");
    await i18n.changeLanguage("en");
    expect(formatBytes(1536 * 1024)).toBe("1.5 MB");
    expect(formatPercent(4.5)).toBe("4.5%");
    await i18n.changeLanguage("ru");
    expect(formatBytes(1536 * 1024)).toBe("1,5 МБ");
  });
});
