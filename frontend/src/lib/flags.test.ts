import {
  COUNTRY_CODES,
  countryOptions,
  flagCode,
  flagCountryName,
  flagEmoji,
  splitFlag,
  withFlag,
} from "./flags";

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

  it("reads the country back from a flag", () => {
    expect(flagCode("🇩🇪")).toBe("DE");
    expect(flagCode("DE")).toBeNull();
    expect(flagCode("")).toBeNull();
    expect(flagCountryName("🇩🇪", "ru")).toBe("Германия");
    expect(flagCountryName("🇩🇪", "en")).toBe("Germany");
    expect(flagCountryName("", "ru")).toBeNull();
  });

  it("has unique two-letter codes", () => {
    expect(new Set(COUNTRY_CODES).size).toBe(COUNTRY_CODES.length);
    expect(COUNTRY_CODES.every((code) => /^[A-Z]{2}$/.test(code))).toBe(true);
  });

  it("names countries in the interface language, sorted for that language", () => {
    const russian = countryOptions("ru");
    expect(russian.find((option) => option.flag === "🇩🇪")?.name).toBe("Германия");
    expect(russian.map((option) => option.name)).toEqual(
      [...russian.map((option) => option.name)].sort((a, b) => a.localeCompare(b, "ru")),
    );
    expect(countryOptions("en").find((option) => option.flag === "🇩🇪")?.name).toBe("Germany");
  });
});
