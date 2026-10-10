import { formatBitRate, formatBytes, formatByteRate, formatPercent, share } from "./units";

describe("units", () => {
  it("shows sizes in binary multiples like the OS does", () => {
    expect(formatBytes(0)).toBe("0 Б");
    expect(formatBytes(1536)).toBe("1,5 КБ");
    expect(formatBytes(586_000_000)).toBe("559 МБ");
    expect(formatBytes(null)).toBe("—");
  });

  it("shows network speed in bits per second like links are sold", () => {
    expect(formatBitRate(125_000)).toBe("1 Мбит/с"); // 125 000 байт/с = 1 000 000 бит/с
    expect(formatBitRate(7822)).toBe("63 Кбит/с");
    expect(formatBitRate(250_000_000)).toBe("2 Гбит/с");
    expect(formatBitRate(undefined)).toBe("—");
  });

  it("shows disk speed and percentages", () => {
    expect(formatByteRate(4096)).toBe("4 КБ/с");
    expect(formatPercent(4.35)).toBe("4,4 %"); // половина округляется вверх; пробел неразрывный, как в Intl
    expect(formatPercent(57.8)).toBe("58 %");
    expect(share(559, 961)).toBeCloseTo(58.17, 1);
    expect(share(1, 0)).toBeNull();
  });
});
