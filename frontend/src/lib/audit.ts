import type { Schemas } from "../api/client";

export type AuditAction = Schemas["AuditAction"];
export type AuditLog = Schemas["AuditLogRead"];

type Tone = "down" | "warn" | null;

interface ActionInfo {
  /** Безличная фраза: кто действовал, показывается отдельно. */
  text: string;
  group: string;
  /** Что требует внимания: ошибки и подозрительное — красным, чувствительное — янтарным. */
  tone?: Tone;
}

const SIGN_IN = "Вход";
const ADMINS = "Админы";
const SERVERS = "Серверы";
const ACCESS = "Пользователи и доступ";
const CONFIGS = "Конфиги";

export const AUDIT_ACTIONS: Record<AuditAction, ActionInfo> = {
  login: { text: "Вход в панель", group: SIGN_IN },
  login_failed: { text: "Неудачная попытка входа", group: SIGN_IN, tone: "warn" },
  login_locked: { text: "Вход заблокирован после серии неудач", group: SIGN_IN, tone: "down" },
  logout: { text: "Выход из панели", group: SIGN_IN },
  admin_created: { text: "Добавлен админ", group: ADMINS },
  admin_updated: { text: "Изменён админ", group: ADMINS },
  server_created: { text: "Добавлен сервер", group: SERVERS },
  server_updated: { text: "Изменён сервер", group: SERVERS },
  server_deleted: { text: "Удалён сервер", group: SERVERS },
  server_credentials_set: { text: "Задан SSH-доступ", group: SERVERS },
  server_credentials_deleted: { text: "Удалён SSH-доступ", group: SERVERS },
  server_host_key_pinned: { text: "Закреплён ключ сервера", group: SERVERS },
  server_host_key_reset: { text: "Сброшен ключ сервера", group: SERVERS },
  server_host_key_mismatch: {
    text: "Ключ сервера не совпал — подключение отклонено",
    group: SERVERS,
    tone: "down",
  },
  server_install_started: { text: "Начата установка Amnezia", group: SERVERS },
  server_install_completed: { text: "Amnezia установлена", group: SERVERS },
  server_install_failed: { text: "Установка Amnezia не удалась", group: SERVERS, tone: "down" },
  server_sync_started: { text: "Начата сверка с базой", group: SERVERS },
  server_sync_completed: { text: "Сверка с базой завершена", group: SERVERS },
  server_sync_failed: { text: "Сверка с базой не удалась", group: SERVERS, tone: "down" },
  server_ssh_key_install_started: { text: "Начата установка ключа панели", group: SERVERS },
  server_ssh_key_install_completed: { text: "Панель перешла на вход по своему ключу", group: SERVERS },
  server_ssh_key_install_failed: { text: "Ключ панели не установлен", group: SERVERS, tone: "down" },
  protocol_install_started: { text: "Начата установка AmneziaWG", group: SERVERS },
  protocol_install_completed: { text: "AmneziaWG установлен", group: SERVERS },
  protocol_install_failed: { text: "Установка AmneziaWG не удалась", group: SERVERS, tone: "down" },
  protocol_remove_started: { text: "Начато снятие AmneziaWG", group: SERVERS },
  protocol_remove_completed: { text: "AmneziaWG снят", group: SERVERS },
  protocol_remove_failed: { text: "Снятие AmneziaWG не удалось", group: SERVERS, tone: "down" },
  vpn_user_created: { text: "Добавлен пользователь", group: ACCESS },
  vpn_user_updated: { text: "Изменён пользователь", group: ACCESS },
  vpn_user_deleted: { text: "Удалён пользователь", group: ACCESS },
  subscription_created: { text: "Выдан доступ", group: ACCESS },
  subscription_updated: { text: "Изменён срок доступа", group: ACCESS },
  subscription_activated: { text: "Доступ начал действовать", group: ACCESS },
  subscription_revoked: { text: "Отозван весь доступ", group: ACCESS },
  subscription_expired: { text: "Доступ истёк", group: ACCESS },
  subscription_deleted: { text: "Удалена запись о доступе", group: ACCESS },
  access_granted: { text: "Сервер добавлен в доступ", group: ACCESS },
  access_updated: { text: "Изменён доступ к серверу", group: ACCESS },
  access_revoked: { text: "Отозван доступ к серверу", group: ACCESS },
  access_expired: { text: "Истёк доступ к серверу", group: ACCESS },
  access_deleted: { text: "Удалён доступ к серверу", group: ACCESS },
  credential_issued: { text: "Выдан конфиг", group: CONFIGS },
  credential_issue_failed: { text: "Конфиг не выдан — ошибка", group: CONFIGS, tone: "down" },
  credential_revoked: { text: "Отозван конфиг", group: CONFIGS },
  credential_revoke_failed: { text: "Конфиг не отозван — ошибка", group: CONFIGS, tone: "down" },
  config_viewed: { text: "Показан конфиг", group: CONFIGS, tone: "warn" },
};

/** Действия по группам — для выбора в фильтре (optgroup), в порядке словаря. */
export function actionGroups(): [string, AuditAction[]][] {
  const groups = new Map<string, AuditAction[]>();
  for (const [action, info] of Object.entries(AUDIT_ACTIONS) as [AuditAction, ActionInfo][]) {
    groups.set(info.group, [...(groups.get(info.group) ?? []), action]);
  }
  return [...groups.entries()];
}

/** Действия одной группы — для фильтра «вся группа». */
export function actionsOfGroup(group: string): AuditAction[] {
  return actionGroups().find(([name]) => name === group)?.[1] ?? [];
}

export interface Names {
  servers: Map<string, string>;
  users: Map<string, string>;
  admins: Map<string, string>;
}

const text = (value: unknown): string | null => (typeof value === "string" && value ? value : null);

/**
 * О ком или о чём запись — по-человечески. Имя берётся из текущих списков, а для удалённых объектов — из данных
 * самой записи (при удалении туда пишется имя). Ничего не нашлось — объект не называется, а не выдумывается.
 */
export function auditSubject(log: AuditLog, names: Names): { label: string; to?: string } | null {
  const meta = log.metadata ?? {};
  const id = log.entity_id;
  switch (log.entity_type) {
    case "server": {
      const name = (id && names.servers.get(id)) ?? text(meta.name);
      if (!name) return null;
      return id && names.servers.has(id) ? { label: name, to: `/servers/${id}` } : { label: name };
    }
    case "vpn_user": {
      const name = (id && names.users.get(id)) ?? text(meta.username);
      if (!name) return null;
      return id && names.users.has(id) ? { label: name, to: `/users/${id}` } : { label: name };
    }
    case "subscription": {
      const userId = text(meta.user_id);
      const name = userId ? names.users.get(userId) : undefined;
      return name && userId ? { label: name, to: `/users/${userId}` } : null;
    }
    case "access_grant":
    case "vpn_credential": {
      const serverId = text(meta.server_id);
      const name = serverId ? names.servers.get(serverId) : undefined;
      return name && serverId ? { label: name, to: `/servers/${serverId}` } : null;
    }
    case "admin": {
      const email = text(meta.email) ?? (id ? names.admins.get(id) : undefined);
      return email ? { label: email } : null;
    }
    default: {
      const email = text(meta.email); // неудачный вход — записи без объекта, но с адресом
      return email ? { label: email } : null;
    }
  }
}

/** Кто действовал: админ по адресу из записи; без админа — анонимный вход (с адреса) или система (планировщик, воркер). */
export function auditActor(log: AuditLog): string {
  if (log.admin_email) return log.admin_email;
  if (log.action === "login_failed" || log.action === "login_locked") {
    return log.ip_address ? `Аноним (${log.ip_address})` : "Аноним";
  }
  if (!log.admin_id) return "Система";
  return `Админ ${log.admin_id.slice(0, 8)}`; // админа уже удалили — остался только id
}

export interface Detail {
  label: string;
  value: string;
}

/** Ключи meta, которые показываем, и их подписи. Всё остальное не выводится: в журнал не должно просочиться лишнее. */
const DETAIL_LABELS: [key: string, label: string][] = [
  ["reason", "Причина"],
  ["error", "Ошибка"],
  ["fingerprint", "Ключ сервера"],
  ["previous", "Прежний ключ"],
  ["expected", "Ожидался ключ"],
  ["presented", "Предъявлен ключ"],
  ["valid_from", "Действует с"],
  ["valid_until", "Действует до"],
  ["scope", "Блокировка"],
  ["lock_seconds", "Блокировка, секунд"],
  ["panel_key_left_on_server", "Ключ панели остался на сервере"],
  ["sessions_revoked", "Завершено сессий"],
];

const show = (value: unknown): string =>
  typeof value === "string" || typeof value === "number" || typeof value === "boolean"
    ? String(value)
    : value === null || value === undefined
      ? "—"
      : JSON.stringify(value);

/** Подробности события для раскрывающейся строки: только ключи из белого списка. Ошибки — обычный текст. */
export function auditDetails(log: AuditLog): Detail[] {
  const meta = log.metadata ?? {};
  const details: Detail[] = [];
  const changes = meta.changes;
  if (changes && typeof changes === "object" && !Array.isArray(changes)) {
    for (const [field, pair] of Object.entries(changes)) {
      const [before, after] = Array.isArray(pair) ? pair : [undefined, pair];
      details.push({ label: "Изменено", value: `${field}: ${show(before)} → ${show(after)}` });
    }
  }
  for (const [key, label] of DETAIL_LABELS) {
    if (key in meta && meta[key] !== null && meta[key] !== undefined) {
      details.push({ label, value: show(meta[key]) });
    }
  }
  return details;
}

export function auditTone(action: AuditAction): Tone {
  return AUDIT_ACTIONS[action]?.tone ?? null;
}
