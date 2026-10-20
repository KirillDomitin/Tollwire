import { json } from "./render";

/** Подменный бэкенд для тестов раздела «Шлюз»: состояние в памяти, все запросы и тела запоминаются. */

export const GW = "11111111-1111-4111-8111-111111111111"; // сервер со шлюзом
export const FRA = "22222222-2222-4222-8222-222222222222"; // уже выход (слот 1)
export const AMS = "33333333-3333-4333-8333-333333333333"; // годится в выходы
export const APP = "44444444-4444-4444-8444-444444444444"; // только приложение AmneziaVPN
export const OTHER_GW = "55555555-5555-4555-8555-555555555555"; // тоже шлюз, обычного протокола нет
export const EXIT1 = "e1e1e1e1-e1e1-41e1-81e1-e1e1e1e1e1e1";
export const EXIT2 = "e2e2e2e2-e2e2-42e2-82e2-e2e2e2e2e2e2";
export const PROTO_GW = "a1a1a1a1-a1a1-41a1-81a1-a1a1a1a1a1a1";
export const LIST_ACTIVE = "b1b1b1b1-b1b1-41b1-81b1-b1b1b1b1b1b1";
export const LIST_CANDIDATE = "b2b2b2b2-b2b2-42b2-82b2-b2b2b2b2b2b2";
export const CRED_ALICE = "c1c1c1c1-c1c1-41c1-81c1-c1c1c1c1c1c1";
export const CRED_BOB = "c2c2c2c2-c2c2-42c2-82c2-c2c2c2c2c2c2";
export const CRED_CAROL = "c3c3c3c3-c3c3-43c3-83c3-c3c3c3c3c3c3";
export const CRED_UNKNOWN = "c4c4c4c4-c4c4-44c4-84c4-c4c4c4c4c4c4";

const NOW = Date.now();
export const ago = (seconds: number) => new Date(NOW - seconds * 1000).toISOString();

export const baseServer = {
  id: GW,
  name: "gw-1",
  host: "203.0.113.10",
  ssh_port: 22,
  status: "ready",
  description: null,
  health_status: "ok",
  last_health_at: ago(60),
  ssh_host_key_fingerprint: "SHA256:abc",
  created_at: "2026-10-01T10:00:00Z",
};

const credential = { id: "c1", server_id: GW, auth_type: "ssh_key", username: "root", created_at: ago(9999) };

export const exit1 = {
  id: EXIT1,
  exit_server_id: FRA,
  exit_server_name: "Frankfurt",
  slot: 1,
  state: "active",
  health: "ok",
  last_handshake_at: ago(120),
  checked_at: ago(30),
  healthy_since: ago(3600),
  weight: 3,
  max_clients: null,
  assigned_clients: 2,
};
export const exit2 = {
  ...exit1,
  id: EXIT2,
  exit_server_id: AMS,
  exit_server_name: "Amsterdam",
  slot: 2,
  weight: 1,
  max_clients: 10,
  assigned_clients: 0,
  last_handshake_at: ago(5),
};

const proto = (id: string, server: string, type: string, port: number | null = 51820) => ({
  id,
  server_id: server,
  type,
  port,
  created_at: "2026-10-01T10:00:00Z",
});

const SUB = (name: string) => `sub-${name}`;
const user = (id: string, username: string) => ({
  id,
  username,
  email: `${username}@example.com`,
  status: "active",
  description: null,
  created_at: "2026-09-01T10:00:00Z",
  access: {
    id: SUB(username),
    status: "active",
    valid_from: ago(86400 * 5),
    valid_until: new Date(NOW + 86400 * 30 * 1000).toISOString(),
  },
});
export const USERS = [user("u-alice", "alice"), user("u-bob", "bob"), user("u-carol", "carol")];
const CREDENTIAL_OF: Record<string, string> = { alice: CRED_ALICE, bob: CRED_BOB, carol: CRED_CAROL };

export const assignmentAlice = {
  credential_id: CRED_ALICE,
  exit_id: EXIT1,
  slot: 1,
  reason: "issue",
  assigned_at: ago(7200),
  pinned_exit_id: null,
};
export const assignmentBob = {
  credential_id: CRED_BOB,
  exit_id: EXIT1,
  slot: 1,
  reason: "failover_return",
  assigned_at: ago(600),
  pinned_exit_id: EXIT1,
};
export const assignmentUnknown = {
  credential_id: CRED_UNKNOWN,
  exit_id: EXIT1,
  slot: 1,
  reason: "sync",
  assigned_at: ago(100),
  pinned_exit_id: null,
};

export const activeList = {
  id: LIST_ACTIVE,
  source: "ripe",
  version: "20261019",
  sha256: "ab".repeat(32),
  prefixes_count: 8612,
  addresses_count: 30_000_000,
  status: "active",
  created_at: ago(86400 * 3),
  activated_at: ago(86400 * 3),
};
export const candidateList = {
  ...activeList,
  id: LIST_CANDIDATE,
  version: "20261026",
  prefixes_count: 4100,
  status: "candidate",
  activated_at: null,
};
export const overrideDirect = {
  id: "o1",
  network: "5.45.200.0/24",
  action: "direct",
  note: "Яндекс за CDN",
  created_at: ago(1000),
};

export interface Backend {
  permissions?: string[];
  protocols?: unknown[];
  jobs?: unknown[];
  exits?: Record<string, unknown>[];
  assignments?: Record<string, unknown>[];
  active?: unknown | null;
  candidates?: unknown[];
  overrides?: Record<string, unknown>[];
  /** Ответ на DELETE выхода без force: 202 по умолчанию. */
  removeStatus?: number;
  removeDetail?: string;
  /** Ответы на остальные изменения: статус и текст detail. */
  fail?: Record<string, { status: number; detail: unknown }>;
  /** Результат задачи обновления списка. */
  updateResult?: Record<string, unknown>;
}

const job = (type: string, extra: Record<string, unknown> = {}) => ({
  id: `job-${type}`,
  type,
  status: "pending",
  progress: 0,
  server_id: GW,
  created_at: new Date().toISOString(),
  started_at: null,
  finished_at: null,
  error: null,
  result: null,
  ...extra,
});

/** Подменный fetch: GET отдают состояние, изменения его меняют. calls — «МЕТОД путь?query», bodies — тела по тем же ключам. */
export function mockGateway(options: Backend = {}) {
  const calls: string[] = [];
  const bodies: Record<string, unknown[]> = {};
  const state = {
    exits: options.exits ?? [exit1],
    assignments: options.assignments ?? [assignmentAlice, assignmentBob],
    active: options.active === undefined ? activeList : options.active,
    candidates: options.candidates ?? [],
    overrides: options.overrides ?? [],
  };
  const protocols = options.protocols ?? [proto("p-gw", GW, "amnezia_gateway", 51821)];
  const protocolsByServer: Record<string, unknown[]> = {
    [GW]: protocols,
    [FRA]: [proto("p-fra", FRA, "amnezia_wg")],
    [AMS]: [proto("p-ams", AMS, "amnezia_wg")],
    [APP]: [proto("p-app", APP, "amnezia_app_awg")],
    [OTHER_GW]: [proto("p-gw2", OTHER_GW, "amnezia_gateway", 51822)],
  };
  const servers = [
    baseServer,
    { ...baseServer, id: FRA, name: "Frankfurt", host: "198.51.100.1" },
    { ...baseServer, id: AMS, name: "Amsterdam", host: "198.51.100.2" },
    { ...baseServer, id: APP, name: "AppOnly", host: "198.51.100.3" },
    { ...baseServer, id: OTHER_GW, name: "gw-2", host: "198.51.100.4" },
  ];
  const failure = (key: string) => {
    const failed = options.fail?.[key];
    return failed ? json({ detail: failed.detail }, failed.status) : null;
  };

  const handler = async (request: Request): Promise<Response> => {
    const url = new URL(request.url);
    const path = url.pathname;
    const key = `${request.method} ${path}${url.search}`;
    calls.push(key);
    const readBody = async () => {
      const text = await request.text();
      (bodies[`${request.method} ${path}`] ??= []).push(
        request.headers.get("content-type")?.includes("json") ? JSON.parse(text) : text,
      );
      return text ? (request.headers.get("content-type")?.includes("json") ? JSON.parse(text) : text) : null;
    };
    const base = `/api/v1/servers/${GW}`;
    const method = request.method;

    if (method === "GET") {
      if (path === base) return json(baseServer);
      if (path === `${base}/credentials`) return json(credential);
      if (path === `${base}/discovery`) return json({ checked_at: null, protocols: [] });
      if (path === "/api/v1/servers") return json(servers);
      const protocolsMatch = /^\/api\/v1\/servers\/([^/]+)\/protocols$/.exec(path);
      if (protocolsMatch) return json(protocolsByServer[protocolsMatch[1] ?? ""] ?? []);
      if (path === "/api/v1/jobs") return json(options.jobs ?? []);
      if (path === `${base}/gateway/exits`) return json(state.exits);
      if (path === `${base}/gateway/assignments`) return json(state.assignments);
      if (path === "/api/v1/geoip") return json({ active: state.active, candidates: state.candidates });
      if (path === "/api/v1/geoip/overrides") return json(state.overrides);
      if (path === "/api/v1/vpn-users") return json(USERS);
      const grantsMatch = /^\/api\/v1\/subscriptions\/sub-([a-z]+)\/grants$/.exec(path);
      if (grantsMatch) {
        return json([
          {
            id: `grant-${grantsMatch[1]}`,
            subscription_id: SUB(grantsMatch[1] ?? ""),
            server_id: GW,
            protocol_id: "p-gw",
            status: "active",
            revoked_at: null,
            created_at: ago(1000),
          },
        ]);
      }
      const credentialsMatch = /^\/api\/v1\/access\/grant-([a-z]+)\/credentials$/.exec(path);
      if (credentialsMatch) {
        return json([
          {
            id: CREDENTIAL_OF[credentialsMatch[1] ?? ""],
            access_grant_id: `grant-${credentialsMatch[1]}`,
            revoked_at: null,
            created_at: ago(900),
            source: "issued",
            external_name: null,
          },
        ]);
      }
      const jobMatch = /^\/api\/v1\/jobs\/(.+)$/.exec(path);
      if (jobMatch) {
        return json(
          job("geoip_update", {
            id: jobMatch[1],
            status: "success",
            server_id: null,
            result: options.updateResult ?? { status: "active", prefixes: 8700, version: "20261026" },
          }),
        );
      }
    }

    if (method === "POST" && path === `${base}/gateway/exits`) {
      const body = (await readBody()) as { exit_server_id: string };
      return failure(key) ?? json(job("gateway_add_exit", { params: body }), 202);
    }
    const exitMatch = new RegExp(`^${base}/gateway/exits/([^/]+)$`).exec(path);
    if (exitMatch && method === "PATCH") {
      const body = (await readBody()) as Record<string, unknown>;
      const failed = failure(key);
      if (failed) return failed;
      state.exits = state.exits.map((item) => (item.id === exitMatch[1] ? { ...item, ...body } : item));
      return json(state.exits.find((item) => item.id === exitMatch[1]));
    }
    if (exitMatch && method === "DELETE") {
      if (!url.searchParams.has("force") && options.removeStatus && options.removeStatus !== 202) {
        return json({ detail: options.removeDetail ?? "conflict" }, options.removeStatus);
      }
      return failure(key) ?? json(job("gateway_remove_exit"), 202);
    }
    const moveMatch = new RegExp(`^${base}/gateway/assignments/([^/]+)$`).exec(path);
    if (moveMatch && method === "PATCH") {
      const body = (await readBody()) as { exit_id: string | null; pin: boolean };
      const failed = failure(key);
      if (failed) return failed;
      const target = state.exits.find((item) => item.id === body.exit_id);
      state.assignments = state.assignments.map((item) =>
        item.credential_id === moveMatch[1]
          ? {
              ...item,
              ...(target ? { exit_id: target.id, slot: target.slot, reason: "manual" } : {}),
              pinned_exit_id: body.pin ? (target?.id ?? item.exit_id) : null,
            }
          : item,
      );
      return json(state.assignments.find((item) => item.credential_id === moveMatch[1]));
    }
    if (method === "POST" && path === `${base}/gateway/check`) {
      return failure(key) ?? json(job("gateway_apply"), 202);
    }
    if (method === "POST" && path === `${base}/gateway/ru/apply`) {
      return failure(key) ?? json(job("gateway_apply_ru"), 202);
    }
    if (method === "POST" && path === "/api/v1/geoip/update") {
      return failure(key) ?? json(job("geoip_update", { id: "job-geoip", server_id: null }), 202);
    }
    if (method === "POST" && path === "/api/v1/geoip/upload") {
      await readBody();
      return (
        failure(key) ??
        json({
          status: "active",
          list_id: LIST_ACTIVE,
          version: "20261030",
          prefixes: 8800,
          sha256: "cd",
          skipped: 1,
          rejected: 0,
        })
      );
    }
    const activateMatch = /^\/api\/v1\/geoip\/([^/]+)\/activate$/.exec(path);
    if (method === "POST" && activateMatch) {
      const failed = failure(key);
      if (failed) return failed;
      state.active = { ...candidateList, status: "active", activated_at: new Date().toISOString() };
      state.candidates = [];
      return json(state.active);
    }
    if (method === "POST" && path === "/api/v1/geoip/overrides") {
      const body = (await readBody()) as Record<string, unknown>;
      const failed = failure(key);
      if (failed) return failed;
      const created = {
        id: `o-${state.overrides.length + 2}`,
        note: null,
        created_at: new Date().toISOString(),
        ...body,
      };
      state.overrides = [...state.overrides, created];
      return json(created, 201);
    }
    const overrideMatch = /^\/api\/v1\/geoip\/overrides\/([^/]+)$/.exec(path);
    if (method === "DELETE" && overrideMatch) {
      const failed = failure(key);
      if (failed) return failed;
      state.overrides = state.overrides.filter((item) => item.id !== overrideMatch[1]);
      return new Response(null, { status: 204 });
    }
    if (method === "POST" && path === `${base}/protocol`) {
      await readBody();
      return failure(key) ?? json(job("install_protocol"), 202);
    }
    if (method === "DELETE" && path === `${base}/protocol`) {
      return failure(key) ?? json(job("remove_protocol"), 202);
    }
    return json({ detail: `unexpected ${key}` }, 500);
  };
  return { calls, bodies, state, handler };
}

export const VIEWER = ["servers:read", "access:read", "vpn_users:read", "jobs:read", "audit:read"];
export const OPERATOR = [...VIEWER, "servers:check", "vpn_users:manage", "access:manage", "config:reveal"];
