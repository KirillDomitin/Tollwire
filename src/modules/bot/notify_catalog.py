"""Explicit notification allowlist; panel_down is consumed only by the bot watchdog."""

from dataclasses import dataclass
from typing import Any

from modules.audit.models.audit_log import AuditAction


@dataclass(frozen=True)
class NotifyEvent:
    title: str
    group: str
    actions: tuple[AuditAction, ...] = ()
    default: bool = True


JOB_ACTIONS = tuple(
    AuditAction(value)
    for value in (
        "server_install_failed",
        "server_sync_failed",
        "server_discover_failed",
        "server_ssh_key_install_failed",
        "protocol_install_failed",
        "protocol_remove_failed",
        "credential_issue_failed",
        "credential_revoke_failed",
        "clients_import_failed",
        "gateway_exit_add_failed",
        "gateway_exit_remove_failed",
        "gateway_ru_apply_failed",
    )
)
CATALOG = {
    "server_down": NotifyEvent("Сервер недоступен", "servers", (AuditAction.SERVER_HEALTH_CHANGED,)),
    "server_recovered": NotifyEvent("Сервер снова доступен", "servers", (AuditAction.SERVER_HEALTH_CHANGED,)),
    "gateway_all_exits_down": NotifyEvent(
        "Все выходы шлюза недоступны", "gateway", (AuditAction.GATEWAY_ALL_EXITS_DOWN,)
    ),
    "gateway_failover": NotifyEvent("Отказ выхода шлюза", "gateway", (AuditAction.GATEWAY_FAILOVER,)),
    "gateway_exit_recovered": NotifyEvent("Выход шлюза восстановлен", "gateway", (AuditAction.GATEWAY_EXIT_RECOVERED,)),
    "jobs_failed": NotifyEvent("Сбои задач", "jobs", JOB_ACTIONS),
    "bot_registration_failed": NotifyEvent(
        "Ошибка регистрации через бота", "bot", (AuditAction.BOT_REGISTRATION_FAILED,)
    ),
    "server_host_key_mismatch": NotifyEvent(
        "Подмена ключа SSH-хоста", "security", (AuditAction.SERVER_HOST_KEY_MISMATCH,)
    ),
    "login_locked": NotifyEvent("Вход заблокирован", "security", (AuditAction.LOGIN_LOCKED,), default=False),
    "panel_down": NotifyEvent("Панель недоступна", "panel"),
}


def default_events() -> dict[str, bool]:
    return {key: event.default for key, event in CATALOG.items()}


def event_keys(action: str, meta: dict[str, Any] | None = None) -> list[str]:
    if action == AuditAction.SERVER_HEALTH_CHANGED.value:
        if not isinstance(meta, dict):
            return []
        old, new = meta.get("from"), meta.get("to")
        if old not in (None, "ok", "degraded", "down") or new not in ("ok", "degraded", "down"):
            return []
        if new == "down" and old != "down":
            return ["server_down"]
        if old == "down" and new in ("ok", "degraded"):
            return ["server_recovered"]
        return []
    return [key for key, event in CATALOG.items() if any(item.value == action for item in event.actions)]
