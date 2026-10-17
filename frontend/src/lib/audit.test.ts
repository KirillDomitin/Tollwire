import i18n from "../i18n";
import schemaText from "../api/schema.d.ts?raw";
import {
  AUDIT_ACTIONS,
  AUDIT_GROUPS,
  auditActionText,
  auditGroupLabel,
  actionsOfGroup,
  auditActor,
  auditDetails,
  auditSubject,
  type AuditAction,
  type AuditLog,
  type Names,
} from "./audit";

const SERVER_ID = "11111111-1111-4111-8111-111111111111";
const USER_ID = "22222222-2222-4222-8222-222222222222";

const log = (overrides: Record<string, unknown>): AuditLog =>
  ({
    id: crypto.randomUUID(),
    admin_id: null,
    admin_email: null,
    action: "server_updated",
    entity_type: null,
    entity_id: null,
    metadata: null,
    ip_address: null,
    created_at: "2026-10-10T10:00:00Z",
    ...overrides,
  }) as unknown as AuditLog;

const names = (overrides: Partial<Names> = {}): Names => ({
  servers: new Map(),
  users: new Map(),
  admins: new Map(),
  ...overrides,
});

/** Детали могут быть строками или объектами — важно, что видит пользователь, поэтому сравниваем по тексту. */
const shown = (value: unknown) => JSON.stringify(auditDetails(value as AuditLog));

describe("AUDIT_ACTIONS", () => {
  it("covers every action the API can return", () => {
    const union = /AuditAction: ([^;]+);/.exec(schemaText)?.[1] ?? "";
    const actions = [...union.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]);
    expect(actions.length).toBeGreaterThan(40);
    expect(Object.keys(AUDIT_ACTIONS).sort()).toEqual([...actions].sort());
  });

  it("gives every action a group and a human text in every language", async () => {
    for (const [action, info] of Object.entries(AUDIT_ACTIONS) as [AuditAction, { group: string }][]) {
      expect(AUDIT_GROUPS).toContain(info.group);
      for (const language of ["ru", "en"]) {
        await i18n.changeLanguage(language);
        expect(auditActionText(action)).not.toBe(`audit.actions.${action}`); // перевод есть, а не сам ключ
      }
    }
  });

  it("labels every group in every language", async () => {
    await i18n.changeLanguage("ru");
    expect(auditGroupLabel("servers")).toBe("Серверы");
    await i18n.changeLanguage("en");
    expect(auditGroupLabel("servers")).toBe("Servers");
  });
});

describe("AmneziaVPN discovery and takeover events", () => {
  it("are grouped with servers, and a taken-over client with configs; failures are red", () => {
    for (const action of [
      "server_discover_started",
      "server_discover_completed",
      "server_discover_failed",
      "clients_import_started",
      "clients_import_completed",
      "clients_import_failed",
    ] as const) {
      expect(AUDIT_ACTIONS[action].group).toBe("servers");
    }
    expect(AUDIT_ACTIONS.credential_imported.group).toBe("configs");
    expect(AUDIT_ACTIONS.server_discover_failed.tone).toBe("down");
    expect(AUDIT_ACTIONS.clients_import_failed.tone).toBe("down");
    expect(actionsOfGroup("servers")).toContain("clients_import_completed");
  });

  it("have a text in both languages", async () => {
    await i18n.changeLanguage("ru");
    expect(auditActionText("credential_imported")).toBe("Клиент подхвачен из AmneziaVPN");
    await i18n.changeLanguage("en");
    expect(auditActionText("credential_imported")).toBe("Client taken over from AmneziaVPN");
  });

  it("link the taken-over client's record to its server", () => {
    const subject = auditSubject(
      log({
        action: "credential_imported",
        entity_type: "vpn_credential",
        entity_id: crypto.randomUUID(),
        metadata: { server_id: SERVER_ID },
      }),
      names({ servers: new Map([[SERVER_ID, "lab-1"]]) }),
    );
    expect(subject).toEqual({ label: "lab-1", to: `/servers/${SERVER_ID}` });
  });
});

describe("actionsOfGroup", () => {
  it("lists the actions of one group", () => {
    const servers = actionsOfGroup("servers");
    expect(servers).toContain("server_created");
    expect(servers).toContain("protocol_install_failed");
    expect(servers).not.toContain("login");
    expect(servers.every((action: AuditAction) => AUDIT_ACTIONS[action].group === "servers")).toBe(true);
  });

  it("is empty for an unknown group", () => {
    expect(actionsOfGroup("nope")).toEqual([]);
  });
});

describe("auditActor", () => {
  it("shows the admin by email", () => {
    expect(auditActor(log({ admin_id: USER_ID, admin_email: "boss@example.com" }))).toBe("boss@example.com");
  });

  it("shows a failed login as anonymous with the address it came from", () => {
    const text = auditActor(log({ action: "login_failed", ip_address: "203.0.113.5" }));
    expect(text).toContain("Аноним");
    expect(text).toContain("203.0.113.5");
  });

  it("shows a lockout as anonymous too", () => {
    expect(auditActor(log({ action: "login_locked", ip_address: "203.0.113.5" }))).toContain("Аноним");
  });

  it("calls a record without an admin the system", () => {
    expect(auditActor(log({ action: "access_expired" }))).toBe("Система");
  });
});

describe("auditSubject", () => {
  it("names a deleted server from the record without a link", () => {
    const subject = auditSubject(
      log({
        action: "server_deleted",
        entity_type: "server",
        entity_id: SERVER_ID,
        metadata: { name: "old-box" },
      }),
      names(),
    );
    expect(subject).toEqual({ label: "old-box" });
  });

  it("links a live server", () => {
    const subject = auditSubject(
      log({ entity_type: "server", entity_id: SERVER_ID }),
      names({ servers: new Map([[SERVER_ID, "lab-1"]]) }),
    );
    expect(subject).toEqual({ label: "lab-1", to: `/servers/${SERVER_ID}` });
  });

  it("names the server of a viewed config", () => {
    const subject = auditSubject(
      log({
        action: "config_viewed",
        entity_type: "vpn_credential",
        entity_id: crypto.randomUUID(),
        metadata: { server_id: SERVER_ID },
      }),
      names({ servers: new Map([[SERVER_ID, "lab-1"]]) }),
    );
    expect(subject).toEqual({ label: "lab-1", to: `/servers/${SERVER_ID}` });
  });

  it("does not invent a name for an unknown id", () => {
    expect(
      auditSubject(log({ entity_type: "server", entity_id: SERVER_ID, metadata: {} }), names()),
    ).toBeNull();
    expect(
      auditSubject(
        log({ action: "config_viewed", entity_type: "vpn_credential", metadata: { server_id: SERVER_ID } }),
        names(),
      ),
    ).toBeNull();
  });

  it("links the user of a subscription event", () => {
    const subject = auditSubject(
      log({ action: "subscription_updated", entity_type: "subscription", metadata: { user_id: USER_ID } }),
      names({ users: new Map([[USER_ID, "anna"]]) }),
    );
    expect(subject).toEqual({ label: "anna", to: `/users/${USER_ID}` });
  });
});

describe("auditDetails", () => {
  it("shows what changed as field: was → became", () => {
    const text = shown(
      log({ metadata: { changes: { name: ["old", "new"], role: ["operator", "viewer"] } } }),
    );
    expect(text).toContain("name: old → new");
    expect(text).toContain("role: operator → viewer");
  });

  it("shows the reason and the error", () => {
    const text = shown(log({ metadata: { reason: "subscription_revoked", error: "ssh timed out" } }));
    expect(text).toContain("subscription_revoked");
    expect(text).toContain("ssh timed out");
  });

  it("shows host key fingerprints", () => {
    const text = shown(
      log({
        metadata: {
          fingerprint: "SHA256:aaa",
          previous: "SHA256:bbb",
          expected: "SHA256:ccc",
          presented: "SHA256:ddd",
        },
      }),
    );
    for (const value of ["SHA256:aaa", "SHA256:bbb", "SHA256:ccc", "SHA256:ddd"])
      expect(text).toContain(value);
  });

  it("shows the access period, login lock scope and leftovers", () => {
    const text = shown(
      log({
        metadata: {
          valid_from: "2026-10-01T00:00:00Z",
          valid_until: "2026-11-01T00:00:00Z",
          scope: "ip",
          lock_seconds: 900,
          panel_key_left_on_server: "SHA256:left",
          sessions_revoked: 3,
        },
      }),
    );
    for (const value of ["2026-10-01", "2026-11-01", "ip", "900", "SHA256:left", "3"])
      expect(text).toContain(value);
  });

  it("never shows keys that are not on the whitelist", () => {
    const text = shown(
      log({
        metadata: {
          reason: "manual",
          private_key: "-----BEGIN PRIVATE KEY-----SECRET",
          password: "hunter2",
          config: "[Interface] PrivateKey = abc",
        },
      }),
    );
    expect(text).toContain("manual");
    for (const secret of ["SECRET", "hunter2", "PrivateKey", "private_key", "[Interface]"]) {
      expect(text).not.toContain(secret);
    }
  });

  it("shows the counters of an AmneziaVPN discovery and takeover, but not the ids", () => {
    const text = shown(
      log({
        action: "clients_import_completed",
        metadata: {
          protocol_id: "99999999-9999-4999-8999-999999999999",
          protocols: 1,
          clients: 4,
          imported: 3,
          already: 1,
          gone: 2,
          unsupported: 5,
          conflict: 6,
          user_id: USER_ID,
          server_id: SERVER_ID,
          access_grant_id: "88888888-8888-4888-8888-888888888888",
        },
      }),
    );
    for (const label of [
      "Найдено установок",
      "Клиентов",
      "Подхвачено",
      "Уже были в панели",
      "Пропали с сервера",
      "Не поддерживаются",
      "Конфликтов",
    ])
      expect(text).toContain(label);
    for (const hidden of ["99999999", USER_ID, SERVER_ID, "88888888"]) expect(text).not.toContain(hidden);
  });

  it("shows the public key of a client taken over, and where a user came from", () => {
    const text = shown(
      log({ action: "credential_imported", metadata: { public_key: "pubkey=", source: "import" } }),
    );
    expect(text).toContain("Публичный ключ клиента");
    expect(text).toContain("pubkey=");
    expect(text).toContain("Источник");
    expect(text).toContain("import");
  });

  it("copes with a record that has no metadata", () => {
    expect(() => auditDetails(log({ metadata: null }))).not.toThrow();
  });
});
