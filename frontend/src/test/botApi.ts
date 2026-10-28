import type { QueryClient } from "@tanstack/react-query";

import { json } from "./render";

/**
 * Подменный бэкенд страницы «Бот» (этап 25.4): состояние в памяти, все запросы запоминаются.
 * Формы ответов - из плана docs/plans/telegram-bot-25-4.md, раздел «Админский API: что добавить».
 * Имена людей и серверов латиницей: английские тесты проверяют, что на экране не осталось кириллицы.
 */

export const USER_ID = "22222222-2222-4222-8222-222222222222";
export const USER2_ID = "23232323-2323-4232-8232-232323232323";
export const USER3_ID = "24242424-2424-4242-8242-242424242424";
export const PROTO_ID = "a1a1a1a1-a1a1-41a1-81a1-a1a1a1a1a1a1";
export const PROTO2_ID = "b2b2b2b2-b2b2-42b2-82b2-b2b2b2b2b2b2";
export const KEY_ID = "c1c1c1c1-c1c1-41c1-81c1-c1c1c1c1c1c1";
export const KEY2_ID = "c2c2c2c2-c2c2-42c2-82c2-c2c2c2c2c2c2";
export const OUTBOX_ID = "d1d1d1d1-d1d1-41d1-81d1-d1d1d1d1d1d1";
export const BROADCAST_ID = "e1e1e1e1-e1e1-41e1-81e1-e1e1e1e1e1e1";
export const BROADCAST2_ID = "e2e2e2e2-e2e2-42e2-82e2-e2e2e2e2e2e2";

/** Открытые секреты: тесты проверяют, что их нет на экране после закрытия окна и в кэше запросов. */
export const KEY_SECRET = "twb_test-only-key-shown-once-0123456789abcdefghijk";
export const INVITE_CODE = "test-only-invite-code-0123";
export const INVITE_LINK = `https://t.me/tollwire_bot?start=${INVITE_CODE}`;
/** Английский текст `detail`, который на экран попадать не должен. */
export const SECRET_DETAIL = "internal detail leak 4242";

export const OPERATOR = ["bot:read", "bot:manage"];
export const VIEWER = ["bot:read"];
export const SUPERADMIN = ["bot:read", "bot:manage", "bot:keys"];

const NOW = Date.now();
export const ago = (seconds: number) => new Date(NOW - seconds * 1000).toISOString();
export const ahead = (seconds: number) => new Date(NOW + seconds * 1000).toISOString();

export const botStatus = (overrides: Record<string, unknown> = {}) => ({
  has_keys: true,
  active_keys: 1,
  bot_username: "tollwire_bot",
  last_seen_at: ago(20),
  last_seen_version: "0.2.0",
  stale: false,
  last_claim_at: ago(5),
  outbox: { pending: 0, claimed: 0 },
  ...overrides,
});

export const botKey = (overrides: Record<string, unknown> = {}) => ({
  id: KEY_ID,
  name: "prod bot",
  key_prefix: "twb_AbCd",
  created_at: ago(86400 * 10),
  last_seen_at: ago(20),
  last_seen_version: "0.2.0",
  bot_username: "tollwire_bot",
  revoked_at: null,
  ...overrides,
});

export const botSettings = (overrides: Record<string, unknown> = {}) => ({
  default_protocol_id: PROTO_ID,
  default_days: 30,
  invite_valid_days: 7,
  panel_down_after_seconds: 300,
  updated_at: ago(3600),
  ...overrides,
});

export const PROTOCOLS = [
  { id: PROTO_ID, server_id: "s1", server_name: "US2", type: "amnezia_wg" },
  { id: PROTO2_ID, server_id: "s2", server_name: "DE1", type: "amnezia_wg" },
];

/** Имя человека в ответе `recipients` - `username` (как в VPNUserRead); `name` добавлено на случай, если бэкенд выберет его. */
export const RECIPIENTS = [
  {
    id: USER_ID,
    username: "Ivan Petrov",
    name: "Ivan Petrov",
    telegram_username: "ivan_tg",
    bot_blocked_at: null,
  },
  {
    id: USER2_ID,
    username: "Anna Blocked",
    name: "Anna Blocked",
    telegram_username: "anna_tg",
    bot_blocked_at: ago(86400),
  },
  {
    id: USER3_ID,
    username: "Olga Smith",
    name: "Olga Smith",
    telegram_username: null,
    bot_blocked_at: null,
  },
];

export const invite = (overrides: Record<string, unknown> = {}) => ({
  id: "i1",
  protocol_id: PROTO_ID,
  days: 30,
  expires_at: ahead(86400 * 5),
  note: "for Ivan",
  created_at: ago(3600),
  used_at: null,
  revoked_at: null,
  status: "valid",
  used_by_user_id: null,
  used_by_name: null,
  ...overrides,
});

export const registration = (overrides: Record<string, unknown> = {}) => ({
  id: "r1",
  invite_id: "i1",
  status: "active",
  name: "Ivan Petrov",
  email: "ivan@example.com",
  telegram_id: 111111111,
  telegram_username: "ivan_tg",
  user_id: USER_ID,
  access_grant_id: "g1",
  error: null,
  created_at: ago(7200),
  finished_at: ago(7100),
  ...overrides,
});

/** Рассылка с согласованными полями: `state` и пара `cancelled_at`/`finished_at` не расходятся. */
export const broadcast = (overrides: Record<string, unknown> = {}) => {
  const state = (overrides.state as string | undefined) ?? "running";
  return {
    id: BROADCAST_ID,
    kind: "news",
    text: "Maintenance tonight",
    target: "all",
    protocol_id: null,
    created_at: ago(600),
    cancelled_at: state === "cancelled" ? ago(60) : null,
    finished_at: state === "running" ? null : ago(30),
    total: 100,
    sent: 60,
    blocked: 5,
    failed: 3,
    skipped: 2,
    cancelled: 0,
    state,
    ...overrides,
  };
};

export interface Call {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
}

type Responder = (call: Call, n: number) => Response | Promise<Response>;

export interface BotBackend {
  /** Права текущего админа (для /auth/me, когда экран открыт через настоящий маршрутизатор). */
  permissions?: string[];
  status?: Record<string, unknown>;
  keys?: Record<string, unknown>[];
  settings?: Record<string, unknown>;
  protocols?: unknown[];
  recipients?: Record<string, unknown>[];
  invites?: Record<string, unknown>[];
  registrations?: Record<string, unknown>[];
  broadcasts?: Record<string, unknown>[];
  audience?: { recipients: number; without_telegram: number; blocked: number };
  /** Ссылка в ответе на создание приглашения; null - имя бота неизвестно. */
  inviteLink?: string | null;
  /** Карточка пользователя USER_ID: поля Telegram и прочее. */
  vpnUser?: Record<string, unknown>;
  /**
   * Свои ответы: ключ «МЕТОД /путь» (`{id}` - любой один сегмент), значение - ответ или функция (запрос, номер вызова
   * этого маршрута с нуля). Проверяются раньше обычных.
   */
  respond?: Record<string, Responder | Response>;
  /** Один ответ на любой запрос к /api/v1/bot-admin/ (загрузка, ошибка), если нет подходящего в `respond`. */
  respondAll?: Responder;
}

const toRegExp = (key: string) =>
  new RegExp(`^${key.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/\{id\}/g, "[^/]+")}$`);

export function mockBot(options: BotBackend = {}) {
  const calls: Call[] = [];
  const unexpected: string[] = [];
  const seen = new Map<string, number>();
  const world = {
    status: botStatus(options.status),
    keys: options.keys ?? [botKey()],
    settings: botSettings(options.settings),
    invites: options.invites ?? [],
    registrations: options.registrations ?? [],
    broadcasts: options.broadcasts ?? [],
  };
  const audience = options.audience ?? { recipients: 12, without_telegram: 3, blocked: 2 };
  const custom = Object.entries(options.respond ?? {}).map(([key, value]) => ({
    method: key.split(" ")[0] ?? "",
    pattern: toRegExp(key.split(" ").slice(1).join(" ")),
    key,
    value,
  }));
  const permissions = options.permissions ?? [
    "servers:read",
    "vpn_users:read",
    "bot:read",
    "bot:manage",
    "bot:keys",
  ];

  const handler = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = input instanceof Request ? input : null;
    const url = new URL(request ? request.url : String(input), window.location.origin);
    const method = (request ? request.method : (init?.method ?? "GET")).toUpperCase();
    const path = url.pathname;
    const text = request ? await request.clone().text() : typeof init?.body === "string" ? init.body : "";
    const body: unknown = text ? (JSON.parse(text) as unknown) : null;
    const call: Call = { method, path, query: url.searchParams, body };
    calls.push(call);

    for (const entry of custom) {
      if (entry.method === method && entry.pattern.test(path)) {
        const n = seen.get(entry.key) ?? 0;
        seen.set(entry.key, n + 1);
        return typeof entry.value === "function" ? entry.value(call, n) : entry.value.clone();
      }
    }
    if (options.respondAll && path.startsWith("/api/v1/bot-admin/")) {
      return options.respondAll(call, calls.length - 1);
    }

    const bot = "/api/v1/bot-admin";
    const match = (pattern: RegExp) => pattern.exec(path);
    const send = (response: Response) => response;

    if (path === "/api/v1/auth/refresh" && method === "POST") return json({ access_token: "test-token" });
    if (path === "/api/v1/auth/me") {
      return json({
        id: "a1",
        email: "admin@example.com",
        is_active: true,
        role: permissions.includes("bot:keys") ? "superadmin" : "operator",
        must_change_password: false,
        created_at: ago(86400 * 100),
        is_superuser: false,
        permissions,
      });
    }

    if (method === "GET") {
      if (path === `${bot}/status`) return json(world.status);
      if (path === `${bot}/keys`) return json(world.keys);
      if (path === `${bot}/settings`) return json(world.settings);
      if (path === `${bot}/protocols`) return json(options.protocols ?? PROTOCOLS);
      if (path === `${bot}/recipients`) {
        const search = (url.searchParams.get("search") ?? "").toLowerCase();
        return json(
          (options.recipients ?? RECIPIENTS).filter((person) =>
            String(person.username).toLowerCase().includes(search),
          ),
        );
      }
      if (path === `${bot}/invites`) return json(world.invites);
      if (path === `${bot}/registrations`) {
        const wanted = url.searchParams.get("status");
        return json(world.registrations.filter((item) => !wanted || item.status === wanted));
      }
      if (path === `${bot}/broadcasts`) return json(world.broadcasts);
      const one = match(new RegExp(`^${bot}/broadcasts/([^/]+)$`));
      if (one)
        return json(world.broadcasts.find((item) => item.id === one[1]) ?? { detail: "Not found" }, 200);
      const outbox = match(new RegExp(`^${bot}/outbox/([^/]+)$`));
      if (outbox) return json({ id: outbox[1], status: "sent", error: null });
      if (path === `/api/v1/vpn-users/${USER_ID}`) {
        return json({
          id: USER_ID,
          username: "alice",
          email: "alice@example.com",
          status: "active",
          description: null,
          created_at: ago(86400 * 40),
          telegram_id: 123456789,
          telegram_username: "ivan_tg",
          bot_blocked_at: null,
          ...options.vpnUser,
        });
      }
      if (path === `/api/v1/vpn-users/${USER_ID}/subscriptions`) return json([]);
      if (path === "/api/v1/servers") return json([]);
    }

    if (method === "POST") {
      if (path === `${bot}/keys`) {
        const created = botKey({
          id: KEY2_ID,
          name: (body as { name: string }).name,
          key_prefix: "twb_test",
        });
        world.keys = [...world.keys, created];
        return send(json({ ...created, key: KEY_SECRET }, 201));
      }
      const revokeKey = match(new RegExp(`^${bot}/keys/([^/]+)/revoke$`));
      if (revokeKey) {
        world.keys = world.keys.map((item) =>
          item.id === revokeKey[1] ? { ...item, revoked_at: ago(0) } : item,
        );
        return json(world.keys.find((item) => item.id === revokeKey[1]));
      }
      if (path === `${bot}/invites`) {
        const asked = body as Record<string, unknown>;
        const created = invite({
          id: "i-new",
          protocol_id: asked.protocol_id ?? PROTO_ID,
          days: asked.days ?? 30,
          expires_at: asked.expires_at ?? ahead(86400 * 7),
          note: asked.note ?? null,
        });
        world.invites = [created, ...world.invites];
        const link = options.inviteLink === undefined ? INVITE_LINK : options.inviteLink;
        return json({ ...created, code: INVITE_CODE, link }, 201);
      }
      const revokeInvite = match(new RegExp(`^${bot}/invites/([^/]+)/revoke$`));
      if (revokeInvite) {
        world.invites = world.invites.map((item) =>
          item.id === revokeInvite[1] ? { ...item, status: "revoked", revoked_at: ago(0) } : item,
        );
        return json(world.invites.find((item) => item.id === revokeInvite[1]));
      }
      const retry = match(new RegExp(`^${bot}/registrations/([^/]+)/retry$`));
      if (retry) {
        world.registrations = world.registrations.map((item) =>
          item.id === retry[1] ? { ...item, status: "provisioning", error: null } : item,
        );
        return json(
          world.registrations.find((item) => item.id === retry[1]),
          202,
        );
      }
      if (path === `${bot}/broadcasts/audience`) return json(audience);
      if (path === `${bot}/broadcasts`) {
        const asked = body as Record<string, unknown>;
        const created = broadcast({
          id: BROADCAST2_ID,
          kind: asked.kind,
          text: asked.text ?? null,
          target: asked.target,
          protocol_id: asked.protocol_id ?? null,
          total: audience.recipients,
          sent: 0,
          blocked: 0,
          failed: 0,
          skipped: 0,
        });
        world.broadcasts = [created, ...world.broadcasts];
        return json(created, 201);
      }
      const cancel = match(new RegExp(`^${bot}/broadcasts/([^/]+)/cancel$`));
      if (cancel) {
        world.broadcasts = world.broadcasts.map((item) =>
          item.id === cancel[1]
            ? { ...item, state: "cancelled", cancelled_at: ago(0), finished_at: ago(0), cancelled: 40 }
            : item,
        );
        return json(world.broadcasts.find((item) => item.id === cancel[1]));
      }
      const link = match(new RegExp(`^${bot}/users/([^/]+)/config-link$`));
      if (link) return json({ outbox_id: OUTBOX_ID, status: "pending" }, 202);
    }

    if (method === "PUT" && path === `${bot}/settings`) {
      world.settings = { ...world.settings, ...(body as Record<string, unknown>) };
      return json(world.settings);
    }

    unexpected.push(`${method} ${path}`);
    return json({ detail: `unexpected request ${method} ${path}` }, 500);
  };

  const stub = vi.fn(handler);
  vi.stubGlobal("fetch", stub);

  /** Запросы «МЕТОД путь» (путь - точный, без запроса). */
  const of = (method: string, path: string) =>
    calls.filter((call) => call.method === method && call.path === path);
  return {
    world,
    calls,
    unexpected,
    handler,
    fetchMock: stub,
    of,
    count: (method: string, path: string) => of(method, path).length,
    last: (method: string, path: string) => of(method, path).at(-1),
  };
}

/** Ответ 429 с Retry-After - как у ограничителя панели. */
export function tooMany(seconds: number): Response {
  return new Response(JSON.stringify({ detail: "Too many requests" }), {
    status: 429,
    headers: { "Content-Type": "application/json", "Retry-After": String(seconds) },
  });
}

/** Ответ 409 с причиной - как у `ReasonConflictError`. */
export const conflict = (reason: string) => json({ detail: SECRET_DETAIL, reason }, 409);

/** Ответ 422 с кодом ошибки Telegram HTML - как у `PydanticCustomError` (без поля `input`). */
export const invalidHtml = (code: string) =>
  json({ detail: [{ type: code, loc: ["body", "text"], msg: SECRET_DETAIL, ctx: { position: 3 } }] }, 422);

/** Запрос, который не отвечает никогда: состояние «загрузка». */
export const never: Responder = () => new Promise<Response>(() => {});

/** Всё, что лежит в кэше запросов и действий: данные ответов. Секрет, показанный один раз, оставаться здесь не должен. */
export function cachedText(queryClient: QueryClient): string {
  return JSON.stringify([
    ...queryClient
      .getMutationCache()
      .getAll()
      .map((mutation) => mutation.state.data),
    ...queryClient
      .getQueryCache()
      .getAll()
      .map((query) => query.state.data),
  ]);
}

/** Пустые состояния разделов (допущение: «<Кого> пока нет» либо «Нет <кого>»). */
export const EMPTY_TEXT = {
  keys: /ключей (пока )?нет|нет ключей/i,
  invites: /приглашений (пока )?нет|нет приглашений/i,
  registrations: /регистраций (пока |с таким статусом )?нет|нет регистраций/i,
  broadcasts: /рассылок (пока )?нет|нет рассылок/i,
};
