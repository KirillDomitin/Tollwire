import { fleetSummary, pick, serverSignal } from "./serverSignal";

describe("serverSignal", () => {
  it.each([
    [{ status: "error", health_status: "ok" }, "down", "Ошибка"], // статус важнее старой проверки
    [{ status: "offline", health_status: null }, "down", "Недоступен"],
    [{ status: "installing", health_status: null }, "process", "Устанавливается"],
    [{ status: "new", health_status: null }, "idle", "Не настроен"],
    [{ status: "ready", health_status: "ok" }, "ok", "В работе"],
    [{ status: "ready", health_status: "degraded" }, "warn", "Есть проблемы"],
    [{ status: "ready", health_status: "down" }, "down", "Не отвечает"],
    [{ status: "ready", health_status: null }, "idle", "Ждёт проверки"],
  ] as const)("%o → %s", (server, signal, label) => {
    expect(serverSignal(server)).toEqual({ signal, label });
  });
});

describe("fleetSummary", () => {
  const ok = { status: "ready", health_status: "ok" } as const;
  const down = { status: "offline", health_status: null } as const;
  const fresh = { status: "new", health_status: null } as const;

  it("puts problems first and agrees words with numbers", () => {
    expect(fleetSummary([ok, ok, down, fresh, fresh])).toBe(
      "5 серверов — 1 недоступен, 2 в работе, 2 не настроены",
    );
  });

  it("says «все» when every server is in the same state", () => {
    expect(fleetSummary([ok, ok, ok])).toBe("3 сервера — все в работе");
    expect(fleetSummary([down])).toBe("1 сервер — недоступен");
  });

  it("handles an empty fleet", () => {
    expect(fleetSummary([])).toBe("Серверов пока нет");
  });

  it("picks Russian plural forms", () => {
    const forms: [string, string, string] = ["сервер", "сервера", "серверов"];
    expect([1, 2, 5, 11, 21, 22, 25].map((n) => pick(n, forms))).toEqual([
      "сервер",
      "сервера",
      "серверов",
      "серверов",
      "сервер",
      "сервера",
      "серверов",
    ]);
  });
});
