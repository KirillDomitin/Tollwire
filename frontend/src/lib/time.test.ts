import { addDays, endOfLocalDay, isValidDay, localDay } from "./time";

// Часовой пояс с переходом на летнее время; Node применяет TZ при записи в process.env (vi.stubEnv пишет туда же).
beforeAll(() => {
  vi.stubEnv("TZ", "Europe/Berlin");
});
afterAll(() => {
  vi.unstubAllEnvs();
});

describe("localDay", () => {
  it("writes the local calendar day, not the UTC one", () => {
    // 00:30 по Берлину (UTC+2 летом) - в UTC это ещё предыдущий день.
    expect(localDay(new Date(2026, 5, 15, 0, 30))).toBe("2026-06-15");
    expect(localDay(new Date(2026, 5, 15, 23, 59))).toBe("2026-06-15");
  });

  it("pads month and day", () => {
    expect(localDay(new Date(2026, 0, 5))).toBe("2026-01-05");
  });
});

describe("addDays", () => {
  it("crosses the end of a month and of a year", () => {
    expect(localDay(addDays(new Date(2026, 0, 31, 12), 1))).toBe("2026-02-01");
    expect(localDay(addDays(new Date(2026, 11, 31, 12), 1))).toBe("2027-01-01");
  });

  it("handles a leap day", () => {
    expect(localDay(addDays(new Date(2028, 1, 28, 12), 1))).toBe("2028-02-29");
    expect(localDay(addDays(new Date(2027, 1, 28, 12), 1))).toBe("2027-03-01");
  });

  it("adds thirty calendar days", () => {
    expect(localDay(addDays(new Date(2026, 9, 17, 12), 30))).toBe("2026-11-16");
  });

  it("counts calendar days across the switch to winter time, where a day has 25 hours", () => {
    // 2026-10-25: в Берлине часы переводят назад. Прибавка в миллисекундах дала бы 26 октября в 23:00 предыдущего дня.
    expect(localDay(addDays(new Date(2026, 9, 24, 12), 1))).toBe("2026-10-25");
    expect(localDay(addDays(new Date(2026, 9, 24, 12), 2))).toBe("2026-10-26");
  });

  it("counts calendar days across the switch to summer time, where a day has 23 hours", () => {
    // 2026-03-29: в Берлине часы переводят вперёд.
    expect(localDay(addDays(new Date(2026, 2, 28, 23, 30), 1))).toBe("2026-03-29");
    expect(localDay(addDays(new Date(2026, 2, 29, 12), 1))).toBe("2026-03-30");
  });

  it("does not change the date it was given", () => {
    const start = new Date(2026, 0, 31, 12);
    addDays(start, 5);
    expect(localDay(start)).toBe("2026-01-31");
  });
});

describe("endOfLocalDay", () => {
  it("is 23:59:59 of that day in local time, as an ISO moment with a zone", () => {
    expect(endOfLocalDay("2026-01-31")).toBe(new Date(2026, 0, 31, 23, 59, 59).toISOString());
    // Зима: UTC+1, значит 22:59:59 UTC.
    expect(endOfLocalDay("2026-01-31")).toBe("2026-01-31T22:59:59.000Z");
  });

  it("follows summer time", () => {
    // Лето: UTC+2.
    expect(endOfLocalDay("2026-07-01")).toBe("2026-07-01T21:59:59.000Z");
  });

  it("is right on the day of the clock change", () => {
    // 2026-10-25 в 23:59:59 уже зимнее время (UTC+1).
    expect(endOfLocalDay("2026-10-25")).toBe("2026-10-25T22:59:59.000Z");
    expect(endOfLocalDay("2026-03-29")).toBe("2026-03-29T21:59:59.000Z");
  });

  it("rolls over month boundaries without help: 31 January stays 31 January", () => {
    expect(new Date(endOfLocalDay("2026-01-31")).getDate()).toBe(31);
    expect(new Date(endOfLocalDay("2026-12-31")).getFullYear()).toBe(2026);
  });
});

describe("isValidDay", () => {
  it("accepts real calendar days, including a leap day", () => {
    for (const day of ["2026-10-17", "2028-02-29", "2026-12-31"]) expect(isValidDay(day)).toBe(true);
  });

  it("rejects wrong formats and days that do not exist", () => {
    for (const day of [
      "",
      "20261-01-01",
      "2026-1-1",
      "2027-02-29",
      "2026-02-31",
      "2026-13-01",
      "abc",
      "2026-10-17T10:00",
    ]) {
      expect(isValidDay(day)).toBe(false);
    }
  });
});
