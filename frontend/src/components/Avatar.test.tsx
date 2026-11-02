import { initials } from "./Avatar";

describe("initials", () => {
  it("takes two letters from a name or an email", () => {
    expect(initials("Иван Петров")).toBe("ИП");
    expect(initials("admin@example.com")).toBe("AE");
    expect(initials("alice")).toBe("AL");
    expect(initials("x")).toBe("X");
    expect(initials("")).toBe("?");
  });
});
