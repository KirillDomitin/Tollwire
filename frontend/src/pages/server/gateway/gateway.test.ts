import i18n from "../../../i18n";
import {
  clientsLeft,
  exitView,
  geoipErrorText,
  healthLabel,
  ingestMessage,
  looksLikeCidr,
  parseWhole,
  reasonLabel,
  type GatewayExit,
} from "./gateway";

const exit = (overrides: Partial<GatewayExit> = {}): GatewayExit => ({
  id: "e1",
  exit_server_id: "s1",
  exit_server_name: "Frankfurt",
  slot: 1,
  state: "active",
  health: "ok",
  last_handshake_at: null,
  checked_at: null,
  healthy_since: null,
  weight: 1,
  max_clients: null,
  assigned_clients: 0,
  ...overrides,
});
const idle = { adding: false, removing: false };

describe("exitView", () => {
  it("calls a working exit active and green", () => {
    expect(exitView(exit(), idle)).toEqual({ tone: "ok", state: "active", stalled: null });
    expect(exitView(exit({ health: null }), idle).tone).toBe("ok"); // ещё не сверяли - годится
  });

  it("calls an exit the balancer considers failed an error", () => {
    expect(exitView(exit({ health: "down" }), idle)).toMatchObject({ tone: "down", state: "error" });
    expect(exitView(exit({ health: "stale" }), idle)).toMatchObject({ tone: "down", state: "error" });
  });

  it("only warns while the tunnel has not shaken hands yet", () => {
    expect(exitView(exit({ health: "no_handshake" }), idle)).toMatchObject({ tone: "warn", state: "active" });
  });

  it("shows connecting only while the add task is really running, otherwise a retry", () => {
    for (const state of ["pending", "peer_created"] as const) {
      expect(exitView(exit({ state }), { adding: true, removing: false })).toMatchObject({
        state: "connecting",
        stalled: null,
      });
      expect(exitView(exit({ state }), idle)).toMatchObject({ state: "notConnected", stalled: "add" });
    }
  });

  it("shows removing only while the remove task is really running, otherwise it can be repeated", () => {
    for (const state of ["draining", "removing"] as const) {
      expect(exitView(exit({ state }), { adding: false, removing: true })).toMatchObject({
        state: "removing",
        stalled: null,
      });
      expect(exitView(exit({ state }), idle)).toMatchObject({ state: "notRemoved", stalled: "remove" });
    }
  });
});

describe("clientsLeft", () => {
  it("takes the number of stranded clients from the 409 text", () => {
    expect(
      clientsLeft(
        "3 client(s) of this exit would be left without an exit and without internet; add another exit first or confirm with ?force=true",
      ),
    ).toBe(3);
  });

  it("ignores other conflicts", () => {
    expect(clientsLeft("A task is already running for this exit")).toBeNull();
    expect(clientsLeft(undefined)).toBeNull();
  });
});

describe("parseWhole", () => {
  it("accepts a whole number inside the bounds", () => {
    expect(parseWhole("50", 1, 100)).toBe(50);
    expect(parseWhole(" 1 ", 1, 100)).toBe(1);
    expect(parseWhole("100", 1, 100)).toBe(100);
  });

  it("rejects empty, fractions, signs, letters and out-of-range", () => {
    for (const text of ["", "0", "101", "1.5", "-3", "+3", "1e2", "abc", "1 2"]) {
      expect(parseWhole(text, 1, 100)).toBeNull();
    }
  });
});

describe("looksLikeCidr", () => {
  it("recognises an IPv4 network with a prefix", () => {
    expect(looksLikeCidr("5.45.200.0/24")).toBe(true);
    expect(looksLikeCidr(" 5.45.200.0/24 ")).toBe(true);
  });

  it("rejects a bare address, IPv6 and text", () => {
    for (const text of ["5.45.200.0", "2001:db8::/32", "network", "", "5.45.200/24"]) {
      expect(looksLikeCidr(text)).toBe(false);
    }
  });
});

describe("geoipErrorText", () => {
  it("explains every refusal of the backend in plain words, in both languages", async () => {
    const cases: [string, RegExp, RegExp][] = [
      ["Network must be an IPv4 CIDR such as 5.45.200.0/24", /формате CIDR/, /CIDR form/],
      ["Network must be an IPv4 CIDR with the host bits zero", /лишние биты хоста/, /host bits/],
      [
        "A direct network must be public (no private, reserved or multicast ranges) and not larger than /16",
        /публичную сеть не крупнее \/16/,
        /public network no larger than \/16/,
      ],
      [
        "A tunnel network must not be larger than /8",
        /не может быть крупнее \/8/,
        /cannot be larger than \/8/,
      ],
      [
        "A direct network must not cover the address of a gateway or an exit server",
        /накрывает адрес шлюза или сервера-выхода/,
        /covers the address of the gateway or an exit server/,
      ],
      [
        "The list contains a network that cannot be routed directly (private, reserved or larger than /12)",
        /частные, зарезервированные сети/,
        /private or reserved networks/,
      ],
      [
        "Too few valid records in the list: 12, at least 1000 expected",
        /слишком мало/,
        /too few suitable records/,
      ],
      [
        "The list has 40000 prefixes after merging, expected from 3000 to 30000",
        /40000.*от 3000 до 30000/,
        /40000 ranges.*3000 to 30000/,
      ],
    ];
    for (const [detail, ru, en] of cases) {
      await i18n.changeLanguage("ru");
      expect(geoipErrorText(detail)).toMatch(ru);
      await i18n.changeLanguage("en");
      expect(geoipErrorText(detail)).toMatch(en);
    }
  });

  it("never shows a raw backend phrase it does not know", () => {
    expect(geoipErrorText("Something odd happened")).not.toContain("odd");
    expect(geoipErrorText(undefined)).toMatch(/не принят/);
  });
});

describe("labels", () => {
  it("translates known health and reasons and passes unknown values through", () => {
    expect(healthLabel("no_handshake")).toBe("рукопожатия ещё не было");
    expect(healthLabel(null)).toBe("не проверялся");
    expect(healthLabel("weird")).toBe("weird");
    expect(reasonLabel("failover_return")).toBe("возврат на закреплённый выход");
    expect(reasonLabel("brand_new")).toBe("brand_new");
  });

  it("describes the result of a list ingest", () => {
    expect(ingestMessage({ status: "active", version: "20261019", prefixes: 8612 })).toMatch(
      /активен.*20261019/,
    );
    expect(ingestMessage({ status: "candidate", version: "20261019", prefixes: 5000 })).toMatch(
      /подтвердите/,
    );
    expect(ingestMessage({ status: "unchanged", version: "20261019", prefixes: 8612 })).toMatch(
      /не изменился/,
    );
    expect(ingestMessage({ status: "active", version: "1", prefixes: 2, rejected: 3 })).toMatch(
      /Пропущены 3 сети/,
    );
  });
});
