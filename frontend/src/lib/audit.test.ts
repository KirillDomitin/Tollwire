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

describe("gateway and Russia-direct events", () => {
  const GATEWAY_EVENTS = [
    "gateway_exit_add_started",
    "gateway_exit_add_completed",
    "gateway_exit_add_failed",
    "gateway_exit_remove_started",
    "gateway_exit_remove_completed",
    "gateway_exit_remove_failed",
    "gateway_exit_updated",
    "gateway_client_assigned",
    "gateway_client_unassigned",
    "gateway_client_moved",
    "gateway_failover",
    "gateway_all_exits_down",
    "gateway_exit_recovered",
    "gateway_container_recreated",
    "gateway_ru_applied",
    "gateway_ru_apply_failed",
    "geoip_list_updated",
    "geoip_list_activated",
    "geoip_override_added",
    "geoip_override_removed",
  ] as const;

  it("are all known, in one group of their own that can be filtered", () => {
    for (const action of GATEWAY_EVENTS) expect(AUDIT_ACTIONS[action].group).toBe("gateway");
    expect(AUDIT_GROUPS).toContain("gateway");
    expect([...actionsOfGroup("gateway")].sort()).toEqual([...GATEWAY_EVENTS].sort());
  });

  it("are red when an operation fails or no exit is left, amber when clients lose their way", () => {
    for (const action of [
      "gateway_exit_add_failed",
      "gateway_exit_remove_failed",
      "gateway_ru_apply_failed",
      "gateway_all_exits_down",
    ] as const) {
      expect(AUDIT_ACTIONS[action].tone).toBe("down");
    }
    for (const action of [
      "gateway_client_unassigned",
      "gateway_failover",
      "gateway_container_recreated",
    ] as const) {
      expect(AUDIT_ACTIONS[action].tone).toBe("warn");
    }
    for (const action of [
      "gateway_exit_add_completed",
      "gateway_client_moved",
      "geoip_list_activated",
    ] as const) {
      expect(AUDIT_ACTIONS[action].tone).toBeUndefined();
    }
  });

  it("have a text and a group name in both languages that does not repeat the key", async () => {
    for (const language of ["ru", "en"]) {
      await i18n.changeLanguage(language);
      for (const action of GATEWAY_EVENTS) expect(auditActionText(action)).not.toContain("audit.actions");
    }
    await i18n.changeLanguage("ru");
    expect(auditGroupLabel("gateway")).toBe("Шлюз");
    expect(auditActionText("gateway_failover")).toBe("Выход шлюза отказал — клиенты перенесены");
    await i18n.changeLanguage("en");
    expect(auditGroupLabel("gateway")).toBe("Gateway");
    expect(auditActionText("geoip_override_added")).toBe("Russia direct exception added");
  });

  it("show slots and counters of a failover in words, but no ids", () => {
    const text = shown(
      log({
        action: "gateway_failover",
        metadata: {
          exit_id: "99999999-9999-4999-8999-999999999999",
          slot: 2,
          moved: 5,
          unassigned: 1,
        },
      }),
    );
    expect(text).toContain("Слот выхода");
    expect(text).toContain("Перенесено клиентов");
    expect(text).toContain("Осталось без выхода");
    expect(text).not.toContain("99999999");
  });

  it("show where a client moved and why, with the reason translated", () => {
    const text = shown(
      log({
        action: "gateway_client_moved",
        metadata: {
          credential_id: "77777777-7777-4777-8777-777777777777",
          from_slot: 1,
          to_slot: 2,
          reason: "failover_return",
        },
      }),
    );
    expect(text).toContain("С выхода");
    expect(text).toContain("На выход");
    expect(text).toContain("возврат на закреплённый выход");
    expect(text).not.toContain("77777777");
  });

  it("translate the exception direction and the list status, and show the list counters, but not the list", () => {
    const text = shown(
      log({
        action: "geoip_list_updated",
        metadata: {
          version: "20261019",
          count: 8612,
          sha256: "ab".repeat(32),
          status: "candidate",
          source: "ripe",
        },
      }),
    );
    for (const expected of [
      "Версия списка",
      "20261019",
      "Диапазонов",
      "8612",
      "Контрольная сумма",
      "ждёт подтверждения",
      "ripe",
    ]) {
      expect(text).toContain(expected);
    }
    const override = shown(
      log({ action: "geoip_override_added", metadata: { network: "5.45.200.0/24", action: "tunnel" } }),
    );
    expect(override).toContain("5.45.200.0/24");
    expect(override).toContain("через выход");
  });

  it("show which protocol was installed or removed, and the routes of an applied list", () => {
    expect(
      shown(log({ action: "protocol_install_completed", metadata: { protocol_type: "amnezia_gateway" } })),
    ).toContain("Шлюз");
    expect(
      shown(log({ action: "protocol_install_completed", metadata: { protocol_type: "amnezia_wg" } })),
    ).toContain("AmneziaWG");
    expect(shown(log({ action: "gateway_ru_applied", metadata: { job_id: "x", routes: 8612 } }))).toContain(
      "Маршрутов на шлюзе",
    );
  });

  it("show the gateway reason in English too, and an unknown value as it is", async () => {
    await i18n.changeLanguage("en");
    expect(
      shown(log({ action: "gateway_container_recreated", metadata: { reason: "sysctl_mismatch" } })),
    ).toContain("outdated kernel settings");
    expect(shown(log({ action: "gateway_client_moved", metadata: { reason: "brand_new" } }))).toContain(
      "brand_new",
    );
  });

  it("leave the reason of other events as the backend sent it", () => {
    expect(shown(log({ action: "login_locked", metadata: { reason: "manual" } }))).toContain("manual");
  });

  it("name the gateway server in the subject, and say nothing for list events", () => {
    const subject = auditSubject(
      log({
        action: "gateway_exit_add_completed",
        entity_type: "server",
        entity_id: SERVER_ID,
        metadata: { slot: 1 },
      }),
      names({ servers: new Map([[SERVER_ID, "gw-1"]]) }),
    );
    expect(subject).toEqual({ label: "gw-1", to: `/servers/${SERVER_ID}` });
    expect(
      auditSubject(
        log({ action: "geoip_list_updated", entity_type: "geoip", entity_id: crypto.randomUUID() }),
        names(),
      ),
    ).toBeNull();
  });
});

describe("config link events (stage 25.3)", () => {
  it("config_link_created is a bot event with a text in every language", async () => {
    expect(AUDIT_ACTIONS.config_link_created.group).toBe("bot");
    for (const language of ["ru", "en"]) {
      await i18n.changeLanguage(language);
      expect(auditActionText("config_link_created")).not.toBe("audit.actions.config_link_created");
    }
  });
});

describe("broadcast and bot settings events (stage 25.4)", () => {
  const BROADCAST_EVENTS = [
    "bot_broadcast_created",
    "bot_broadcast_cancelled",
    "bot_config_link_queued",
    "bot_settings_updated",
  ] as const;
  // До перегенерации схемы этих действий нет в AuditAction: читаем таблицу как обычную карту.
  const info = (action: string) =>
    (AUDIT_ACTIONS as Record<string, { group: string; tone?: string | null } | undefined>)[action];
  const text = (action: string) => auditActionText(action as unknown as AuditAction);
  const asLog = (action: string, extra: Record<string, unknown> = {}) => log({ action, ...extra });

  it("are known and belong to the bot group", () => {
    for (const action of BROADCAST_EVENTS) expect(info(action)?.group, action).toBe("bot");
  });

  it("can be picked with the rest of the bot group in the journal filter", () => {
    for (const action of BROADCAST_EVENTS) expect(actionsOfGroup("bot")).toContain(action);
  });

  it("have a human text in every language, not the key", async () => {
    for (const language of ["ru", "en"]) {
      await i18n.changeLanguage(language);
      for (const action of BROADCAST_EVENTS) {
        expect(text(action), `${language} ${action}`).not.toBe(`audit.actions.${action}`);
        expect(text(action).length).toBeGreaterThan(5);
      }
    }
  });

  it("have English texts without a trace of Russian, and Russian texts in Russian", async () => {
    await i18n.changeLanguage("en");
    for (const action of BROADCAST_EVENTS) expect(text(action)).not.toMatch(/[А-Яа-яЁё]/);
    await i18n.changeLanguage("ru");
    for (const action of BROADCAST_EVENTS) expect(text(action)).toMatch(/[А-Яа-яЁё]/);
  });

  it("show the kind, the audience and the number of recipients in words, but no ids", () => {
    const details = auditDetails(
      asLog("bot_broadcast_created", {
        metadata: {
          kind: "news",
          target: "protocol",
          protocol_id: "99999999-9999-4999-8999-999999999999",
          recipients: 12,
        },
      }),
    );
    const shownText = JSON.stringify(details);

    expect(details.length).toBeGreaterThanOrEqual(3);
    for (const detail of details) expect(detail.label).not.toContain("audit.details"); // подпись переведена
    expect(shownText).toMatch(/news|новост/i);
    expect(shownText).toMatch(/protocol|протокол/i);
    expect(shownText).toContain("12");
    expect(shownText).not.toContain("99999999");
  });

  it("show how many messages a cancellation stopped", () => {
    const details = auditDetails(asLog("bot_broadcast_cancelled", { metadata: { cancelled: 40 } }));

    expect(details).toHaveLength(1);
    expect(details[0]?.value).toBe("40");
    expect(details[0]?.label).not.toContain("audit.details");
  });

  it("show settings that changed as field: was → became", () => {
    const shownText = JSON.stringify(
      auditDetails(asLog("bot_settings_updated", { metadata: { changes: { default_days: [30, 14] } } })),
    );

    expect(shownText).toContain("default_days: 30 → 14");
  });

  it("never show the text of a news item, a Telegram id, an address, a token or the list of people", () => {
    const shownText = JSON.stringify(
      auditDetails(
        asLog("bot_broadcast_created", {
          metadata: {
            kind: "news",
            recipients: 3,
            text: "SECRET-NEWS-TEXT",
            telegram_id: 123456789,
            config_url: "https://vpn.example.com/c#SECRET-TOKEN",
            token: "SECRET-TOKEN",
            user_ids: ["SECRET-USER-ID"],
            outbox_id: "SECRET-OUTBOX-ID",
          },
        }),
      ),
    );

    for (const secret of [
      "SECRET-NEWS-TEXT",
      "123456789",
      "SECRET-TOKEN",
      "SECRET-USER-ID",
      "SECRET-OUTBOX-ID",
    ]) {
      expect(shownText).not.toContain(secret);
    }
  });

  it("name the person of a queued config link and link to their card", () => {
    const subject = auditSubject(
      asLog("bot_config_link_queued", {
        entity_type: "vpn_user",
        entity_id: USER_ID,
        metadata: { outbox_id: "o1" },
      }),
      names({ users: new Map([[USER_ID, "anna"]]) }),
    );

    expect(subject).toEqual({ label: "anna", to: `/users/${USER_ID}` });
  });
});
