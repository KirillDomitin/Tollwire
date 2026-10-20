import type { Schemas } from "../api/client";
import i18n from "../i18n";

export type AuditAction = Schemas["AuditAction"];
export type AuditLog = Schemas["AuditLogRead"];

type Tone = "down" | "warn" | null;

/** Группы событий — стабильные идентификаторы (они в адресе страницы: ?group=servers), подписи — в словаре. */
export const AUDIT_GROUPS = ["signIn", "admins", "servers", "access", "configs", "gateway"] as const;
export type AuditGroup = (typeof AUDIT_GROUPS)[number];

interface ActionInfo {
  group: AuditGroup;
  /** Что требует внимания: ошибки и подозрительное — красным, чувствительное — янтарным. */
  tone?: Tone;
}

/** Группа и важность каждого события. Текст события — в словаре (`audit.actions.<action>`), по языку интерфейса. */
export const AUDIT_ACTIONS: Record<AuditAction, ActionInfo> = {
  login: { group: "signIn" },
  login_failed: { group: "signIn", tone: "warn" },
  login_locked: { group: "signIn", tone: "down" },
  logout: { group: "signIn" },
  password_changed: { group: "signIn" },
  password_change_failed: { group: "signIn", tone: "warn" },
  admin_created: { group: "admins" },
  admin_updated: { group: "admins" },
  server_created: { group: "servers" },
  server_updated: { group: "servers" },
  server_deleted: { group: "servers" },
  server_credentials_set: { group: "servers" },
  server_credentials_deleted: { group: "servers" },
  server_host_key_pinned: { group: "servers" },
  server_host_key_reset: { group: "servers" },
  server_host_key_mismatch: { group: "servers", tone: "down" },
  server_install_started: { group: "servers" },
  server_install_completed: { group: "servers" },
  server_install_failed: { group: "servers", tone: "down" },
  server_sync_started: { group: "servers" },
  server_sync_completed: { group: "servers" },
  server_sync_failed: { group: "servers", tone: "down" },
  server_discover_started: { group: "servers" },
  server_discover_completed: { group: "servers" },
  server_discover_failed: { group: "servers", tone: "down" },
  clients_import_started: { group: "servers" },
  clients_import_completed: { group: "servers" },
  clients_import_failed: { group: "servers", tone: "down" },
  server_ssh_key_install_started: { group: "servers" },
  server_ssh_key_install_completed: { group: "servers" },
  server_ssh_key_install_failed: { group: "servers", tone: "down" },
  protocol_install_started: { group: "servers" },
  protocol_install_completed: { group: "servers" },
  protocol_install_failed: { group: "servers", tone: "down" },
  protocol_remove_started: { group: "servers" },
  protocol_remove_completed: { group: "servers" },
  protocol_remove_failed: { group: "servers", tone: "down" },
  vpn_user_created: { group: "access" },
  vpn_user_updated: { group: "access" },
  vpn_user_deleted: { group: "access" },
  subscription_created: { group: "access" },
  subscription_updated: { group: "access" },
  subscription_activated: { group: "access" },
  subscription_revoked: { group: "access" },
  subscription_expired: { group: "access" },
  subscription_deleted: { group: "access" },
  access_granted: { group: "access" },
  access_updated: { group: "access" },
  access_revoked: { group: "access" },
  access_expired: { group: "access" },
  access_deleted: { group: "access" },
  credential_issued: { group: "configs" },
  credential_imported: { group: "configs" },
  credential_issue_failed: { group: "configs", tone: "down" },
  credential_revoked: { group: "configs" },
  credential_revoke_failed: { group: "configs", tone: "down" },
  config_viewed: { group: "configs", tone: "warn" },
  // Шлюз и «Россия напрямую». Красным — сбои операций и отказ всех выходов, янтарным — то, после чего клиенты теряют
  // связь или остаются без выхода.
  gateway_exit_add_started: { group: "gateway" },
  gateway_exit_add_completed: { group: "gateway" },
  gateway_exit_add_failed: { group: "gateway", tone: "down" },
  gateway_exit_remove_started: { group: "gateway" },
  gateway_exit_remove_completed: { group: "gateway" },
  gateway_exit_remove_failed: { group: "gateway", tone: "down" },
  gateway_exit_updated: { group: "gateway" },
  gateway_client_assigned: { group: "gateway" },
  gateway_client_unassigned: { group: "gateway", tone: "warn" },
  gateway_client_moved: { group: "gateway" },
  gateway_failover: { group: "gateway", tone: "warn" },
  gateway_all_exits_down: { group: "gateway", tone: "down" },
  gateway_exit_recovered: { group: "gateway" },
  gateway_container_recreated: { group: "gateway", tone: "warn" },
  gateway_ru_applied: { group: "gateway" },
  gateway_ru_apply_failed: { group: "gateway", tone: "down" },
  geoip_list_updated: { group: "gateway" },
  geoip_list_activated: { group: "gateway" },
  geoip_override_added: { group: "gateway" },
  geoip_override_removed: { group: "gateway" },
};

/** Безличная фраза о событии на языке интерфейса: кто действовал, показывается отдельно. */
export function auditActionText(action: AuditAction): string {
  return i18n.t(`audit.actions.${action}`);
}

export function auditGroupLabel(group: AuditGroup): string {
  return i18n.t(`audit.groups.${group}`);
}

/** Действия по группам — для выбора в фильтре (optgroup), в порядке словаря. */
export function actionGroups(): [AuditGroup, AuditAction[]][] {
  const groups = new Map<AuditGroup, AuditAction[]>();
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
    return log.ip_address
      ? i18n.t("audit.actor.anonymousFrom", { ip: log.ip_address })
      : i18n.t("audit.actor.anonymous");
  }
  if (!log.admin_id) return i18n.t("audit.actor.system");
  return i18n.t("audit.actor.adminDeleted", { id: log.admin_id.slice(0, 8) }); // админа уже удалили — остался только id
}

export interface Detail {
  label: string;
  value: string;
}

/** Ключи meta, которые показываем, и их подписи. Всё остальное не выводится: в журнал не должно просочиться лишнее. */
const DETAIL_KEYS = [
  "reason",
  "error",
  "fingerprint",
  "previous",
  "expected",
  "presented",
  "valid_from",
  "valid_until",
  "scope",
  "lock_seconds",
  "panel_key_left_on_server",
  "sessions_revoked",
  // Поиск и перенос клиентов AmneziaVPN: только счётчики и публичный ключ. Идентификаторы (protocol_id, user_id,
  // access_grant_id, server_id) не выводятся: они нужны для ссылок на объекты, а не для чтения глазами.
  "protocols",
  "clients",
  "imported",
  "already",
  "gone",
  "unsupported",
  "conflict",
  "public_key",
  "source",
  // Шлюз: слоты выходов, счётчики переносов, число маршрутов и сведения о списке «России напрямую». Идентификаторы
  // выходов, серверов и клиентов (exit_id, exit_server_id, credential_id) не выводятся, как и остальные id.
  "protocol_type",
  "slot",
  "from_slot",
  "to_slot",
  "moved",
  "unassigned",
  "routes",
  "version",
  "count",
  "sha256",
  "status",
  "network",
  "action",
  "weight",
  "force",
  "exits",
] as const;

/** Ключи, значения которых — слова бэкенда (enum): их показываем по-человечески, если перевод есть. */
const TRANSLATED_VALUES = ["protocol_type", "action", "status", "reason", "force"] as const;
/** «reason» есть и у других событий (там это слова с другим смыслом): переводится только у шлюзовых. */
const GATEWAY_ONLY_VALUES = ["reason"];

const show = (value: unknown): string =>
  typeof value === "string" || typeof value === "number" || typeof value === "boolean"
    ? String(value)
    : value === null || value === undefined
      ? "—"
      : JSON.stringify(value);

/** Значение подробности: слова из enum бэкенда переводятся, незнакомые и числа показываются как есть. */
function detailValue(action: AuditAction, key: (typeof DETAIL_KEYS)[number], value: unknown): string {
  const plain = show(value);
  if (!(TRANSLATED_VALUES as readonly string[]).includes(key)) return plain;
  if (GATEWAY_ONLY_VALUES.includes(key) && !action.startsWith("gateway_")) return plain;
  const path = `audit.values.${key}.${plain}`;
  return i18n.exists(path) ? i18n.t(path as "audit.values.force.true") : plain;
}

/** Подробности события для раскрывающейся строки: только ключи из белого списка. Ошибки — обычный текст. */
export function auditDetails(log: AuditLog): Detail[] {
  const meta = log.metadata ?? {};
  const details: Detail[] = [];
  const changes = meta.changes;
  if (changes && typeof changes === "object" && !Array.isArray(changes)) {
    for (const [field, pair] of Object.entries(changes)) {
      const [before, after] = Array.isArray(pair) ? pair : [undefined, pair];
      details.push({
        label: i18n.t("audit.details.changed"),
        value: `${field}: ${show(before)} → ${show(after)}`,
      });
    }
  }
  for (const key of DETAIL_KEYS) {
    if (key in meta && meta[key] !== null && meta[key] !== undefined) {
      details.push({ label: i18n.t(`audit.details.${key}`), value: detailValue(log.action, key, meta[key]) });
    }
  }
  return details;
}

export function auditTone(action: AuditAction): Tone {
  return AUDIT_ACTIONS[action]?.tone ?? null;
}
