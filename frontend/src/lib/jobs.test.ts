import schemaText from "../api/schema.d.ts?raw";
import i18n from "../i18n";
import { jobLabel } from "./jobs";

describe("jobLabel", () => {
  const types = [...(/JobType: ([^;]+);/.exec(schemaText)?.[1] ?? "").matchAll(/"([a-z_]+)"/g)].map(
    (match) => match[1],
  );

  it("knows every task type the API can return, in both languages", async () => {
    expect(types).toEqual(
      expect.arrayContaining([
        "gateway_add_exit",
        "gateway_remove_exit",
        "gateway_apply",
        "gateway_apply_ru",
        "geoip_update",
      ]),
    );
    for (const language of ["ru", "en"]) {
      await i18n.changeLanguage(language);
      for (const type of types) {
        expect(jobLabel(type as Parameters<typeof jobLabel>[0])).not.toBe(`jobs.types.${type}`);
      }
    }
  });

  it("names the gateway tasks in words", () => {
    expect(jobLabel("gateway_add_exit")).toBe("Добавление выхода шлюза");
    expect(jobLabel("gateway_apply_ru")).toBe("Применение «России напрямую»");
    expect(jobLabel("install_protocol")).toBe("Установка протокола"); // ставится и AmneziaWG, и шлюз
  });
});
