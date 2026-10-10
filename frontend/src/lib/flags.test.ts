import { COUNTRIES, flagEmoji, splitFlag, withFlag } from "./flags";

describe("flags", () => {
  it("builds a flag emoji from a country code", () => {
    expect(flagEmoji("DE")).toBe("🇩🇪");
    expect(flagEmoji("us")).toBe("🇺🇸");
  });

  it("joins flag and name, and leaves a name without flag untouched", () => {
    expect(withFlag("🇩🇪", "frankfurt-1")).toBe("🇩🇪 frankfurt-1");
    expect(withFlag("", "frankfurt-1")).toBe("frankfurt-1");
  });

  it("splits a leading flag off the name", () => {
    expect(splitFlag("🇩🇪 frankfurt-1")).toEqual({ flag: "🇩🇪", rest: "frankfurt-1" });
    expect(splitFlag("🇩🇪frankfurt-1")).toEqual({ flag: "🇩🇪", rest: "frankfurt-1" });
    expect(splitFlag("frankfurt-1 🇩🇪")).toEqual({ flag: "", rest: "frankfurt-1 🇩🇪" });
    expect(splitFlag("US2")).toEqual({ flag: "", rest: "US2" });
  });

  it("has unique two-letter codes", () => {
    const codes = COUNTRIES.map(([code]) => code);
    expect(new Set(codes).size).toBe(codes.length);
    expect(codes.every((code) => /^[A-Z]{2}$/.test(code))).toBe(true);
  });
});
