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

describe("bot dictionary (stage 25.4)", () => {
  const section = (dictionary: unknown, name: string) => (dictionary as Record<string, unknown>)[name];
  const CYRILLIC = /[А-Яа-яЁё]/;
  const leaves = (dictionary: unknown, name: string): [string, string][] => {
    const root = section(dictionary, name);
    if (root === undefined) return []; // словаря ещё нет: пусть падает проверка «словарь есть», а не обход
    return keysOf(root).map((key) => [
      key,
      key.split(".").reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], root) as string,
    ]);
  };
  const flatKeys = (dictionary: unknown, name: string) =>
    section(dictionary, name) === undefined ? [] : keysOf(section(dictionary, name));

  it("exists in both languages and is not a stub", () => {
    expect(section(ru, "bot")).toBeDefined();
    expect(section(en, "bot")).toBeDefined();
    expect(flatKeys(ru, "bot").length).toBeGreaterThan(60);
  });

  it("has the same keys in both languages (plural forms aside)", () => {
    expect(rootKeys({ bot: section(en, "bot") })).toEqual(rootKeys({ bot: section(ru, "bot") }));
  });

  it("has all four Russian plural forms and both English ones for every counted phrase", () => {
    const forms = (dictionary: unknown) => {
      const byRoot = new Map<string, Set<string>>();
      for (const key of flatKeys(dictionary, "bot")) {
        const match = PLURAL_SUFFIX.exec(key);
        if (!match) continue;
        const root = key.replace(PLURAL_SUFFIX, "");
        byRoot.set(root, (byRoot.get(root) ?? new Set()).add(match[1] as string));
      }
      return byRoot;
    };
    const russian = forms(ru);
    const english = forms(en);

    expect(russian.size).toBeGreaterThan(0); // получатели, счётчики
    for (const [root, found] of russian) {
      expect([...found].sort(), `ru ${root}`).toEqual(["few", "many", "one", "other"]);
      expect([...(english.get(root) ?? [])].sort(), `en ${root}`).toEqual(["one", "other"]);
    }
  });

  it("keeps the English bot texts free of Cyrillic", () => {
    const withCyrillic = leaves(en, "bot").filter(([, value]) => CYRILLIC.test(value));

    expect(leaves(en, "bot").length).toBeGreaterThan(60); // словарь есть, а не пуст
    expect(withCyrillic).toEqual([]);
  });

  it("writes the Russian bot texts in Russian, apart from a few product names", () => {
    const all = leaves(ru, "bot");
    const withoutCyrillic = all.filter(([, value]) => !CYRILLIC.test(value));

    expect(all.length).toBeGreaterThan(60);
    expect(withoutCyrillic.length).toBeLessThan(all.length / 10);
  });

  it("names the menu item in both languages", () => {
    const nav = (dictionary: unknown) =>
      section(section(dictionary, "shell"), "nav") as Record<string, string>;

    expect(nav(ru).bot).toBe("Бот");
    expect(nav(en).bot).toBe("Bot");
  });

  it("has the audit texts of the four new events in both languages", () => {
    const actions = (dictionary: unknown) =>
      section(section(dictionary, "audit"), "actions") as Record<string, string | undefined>;
    for (const action of [
      "bot_broadcast_created",
      "bot_broadcast_cancelled",
      "bot_config_link_queued",
      "bot_settings_updated",
    ]) {
      expect(actions(ru)[action], `ru ${action}`).toBeTruthy();
      expect(actions(en)[action], `en ${action}`).toBeTruthy();
      expect(actions(en)[action]).not.toMatch(CYRILLIC);
    }
  });

  it("has the audit detail labels for kind, target, recipients and cancelled in both languages", () => {
    const details = (dictionary: unknown) =>
      section(section(dictionary, "audit"), "details") as Record<string, string | undefined>;
    for (const key of ["kind", "target", "recipients", "cancelled"]) {
      expect(details(ru)[key], `ru ${key}`).toBeTruthy();
      expect(details(en)[key], `en ${key}`).toBeTruthy();
      expect(details(en)[key]).not.toMatch(CYRILLIC);
    }
  });
});
