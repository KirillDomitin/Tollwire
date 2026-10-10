import i18n from "../i18n";
import { fleetSummary, serverSignal } from "./serverSignal";

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
});

describe("in English", () => {
  const ok = { status: "ready", health_status: "ok" } as const;
  const down = { status: "offline", health_status: null } as const;
  const fresh = { status: "new", health_status: null } as const;

  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("labels the states", () => {
    expect(serverSignal(ok).label).toBe("Running");
    expect(serverSignal(down).label).toBe("Unreachable");
    expect(serverSignal({ status: "ready", health_status: null }).label).toBe("Awaiting check");
  });

  it("summarises the fleet with English plurals", () => {
    expect(fleetSummary([])).toBe("No servers yet");
    expect(fleetSummary([down])).toBe("1 server — unreachable");
    expect(fleetSummary([ok, ok, ok])).toBe("3 servers — all running");
    expect(fleetSummary([ok, ok, down, fresh, fresh])).toBe(
      "5 servers — 1 unreachable, 2 running, 2 not set up",
    );
  });
});
