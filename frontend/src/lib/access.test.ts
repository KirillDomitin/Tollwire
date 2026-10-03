import { endOfDay, newPeriod } from "../pages/user/GrantAccessDialog";
import { accessSignal, formatDay } from "./access";

const NOW = new Date("2026-10-04T12:00:00Z");
const days = (n: number) => new Date(NOW.getTime() + n * 24 * 60 * 60 * 1000).toISOString();
const period = (status: "active" | "pending" | "expired" | "revoked", from: number, until: number) => ({
  status,
  valid_from: days(from),
  valid_until: days(until),
});

describe("accessSignal", () => {
  it("says there is no access when the user never had one", () => {
    expect(accessSignal(null, NOW)).toEqual({ signal: "idle", label: "Нет доступа" });
  });

  it("shows working access in green with its last day", () => {
    expect(accessSignal(period("active", -10, 30), NOW)).toEqual({ signal: "ok", label: "До 3 ноября" });
  });

  it("warns a week before the end so access can be extended in time", () => {
    expect(accessSignal(period("active", -20, 3), NOW)).toEqual({
      signal: "warn",
      label: "Кончается 7 октября",
    });
  });

  it("shows access that has not started yet as upcoming", () => {
    expect(accessSignal(period("pending", 2, 32), NOW)).toEqual({ signal: "process", label: "С 6 октября" });
  });

  it("treats a started pending period as working — the scheduler just has not caught up", () => {
    expect(accessSignal(period("pending", -1, 30), NOW).signal).toBe("ok");
  });

  it("reports an ended period as expired even before the scheduler marks it", () => {
    expect(accessSignal(period("active", -40, -1), NOW)).toEqual({
      signal: "idle",
      label: "Истёк 3 октября",
    });
    expect(accessSignal(period("expired", -40, -1), NOW).label).toBe("Истёк 3 октября");
  });

  it("shows revoked access as revoked regardless of dates", () => {
    expect(accessSignal(period("revoked", -1, 30), NOW)).toEqual({ signal: "idle", label: "Отозван" });
  });
});

describe("dates", () => {
  it("adds the year only when it differs from the current one", () => {
    expect(formatDay("2026-11-03T10:00:00Z", NOW)).toBe("3 ноября");
    expect(formatDay("2027-01-15T10:00:00Z", NOW)).toMatch(/^15 января 2027/);
  });

  it("starts a new period a minute ago and ends it after the chosen number of days", () => {
    const { valid_from, valid_until } = newPeriod(30, "", NOW);
    expect(new Date(valid_from).getTime()).toBe(NOW.getTime() - 60_000);
    expect(new Date(valid_until).getTime() - new Date(valid_from).getTime()).toBe(30 * 24 * 60 * 60 * 1000);
  });

  it("ends a period on the last second of the chosen local day", () => {
    const until = new Date(endOfDay("2026-11-03"));
    expect([until.getFullYear(), until.getMonth(), until.getDate(), until.getHours()]).toEqual([
      2026, 10, 3, 23,
    ]);
    expect(newPeriod("date", "2026-11-03", NOW).valid_until).toBe(endOfDay("2026-11-03"));
  });
});
