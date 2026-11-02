"""Каталог событий уведомлений (этап 25.5, `modules.bot.notify_catalog`) и настройки сборщика: чистые функции."""

from typing import Any

import pytest
from pydantic import ValidationError

from core.config import Settings
from tests.bot_notify_support import ALL_KEYS, LOGIN_LOCKED, ON_BY_DEFAULT, PANEL_DOWN, SERVER_DOWN, SERVER_RECOVERED


def keys(action: str, meta: dict[str, Any] | None = None) -> list[str]:
    from modules.bot.notify_catalog import event_keys

    return event_keys(action, meta)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("ok", "down", [SERVER_DOWN]),
        ("degraded", "down", [SERVER_DOWN]),
        (None, "down", [SERVER_DOWN]),
        ("down", "ok", [SERVER_RECOVERED]),
        ("down", "degraded", [SERVER_RECOVERED]),
        ("ok", "degraded", []),
        ("degraded", "ok", []),
        (None, "ok", []),
        (None, "degraded", []),
    ],
)
def test_server_health_change_notifies_only_when_the_server_goes_down_or_comes_back(
    old: str | None, new: str, expected: list[str]
) -> None:
    assert keys("server_health_changed", {"from": old, "to": new}) == expected


def test_down_to_down_is_not_an_event() -> None:
    assert keys("server_health_changed", {"from": "down", "to": "down"}) == []


def test_health_change_without_meta_does_not_crash_and_does_not_notify() -> None:
    assert keys("server_health_changed", None) == []
    assert keys("server_health_changed", {}) == []


@pytest.mark.parametrize("meta", [{"from": "down", "to": None}, {"from": [], "to": "down"}, {"to": {}}])
def test_malformed_health_status_does_not_notify(meta: dict[str, Any]) -> None:
    assert keys("server_health_changed", meta) == []


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("gateway_all_exits_down", "gateway_all_exits_down"),
        ("gateway_failover", "gateway_failover"),
        ("gateway_exit_recovered", "gateway_exit_recovered"),
        ("bot_registration_failed", "bot_registration_failed"),
        ("server_host_key_mismatch", "server_host_key_mismatch"),
        ("login_locked", "login_locked"),
    ],
)
def test_single_action_events_map_to_their_catalog_key(action: str, expected: str) -> None:
    assert keys(action) == [expected]


@pytest.mark.parametrize(
    "action",
    [
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
    ],
)
def test_every_failed_job_action_is_a_jobs_failed_event(action: str) -> None:
    assert keys(action) == ["jobs_failed"]


@pytest.mark.parametrize(
    "action",
    [
        "login",
        "server_created",
        "bot_admin_link_created",
        "bot_admin_link_removed",
        "bot_admin_link_code_created",
        "bot_notify_test_queued",
        "bot_broadcast_created",
    ],
)
def test_other_actions_never_notify(action: str) -> None:
    """Само управление уведомлениями (`bot_admin_link_*`) в каталог не входит."""
    assert keys(action) == []


def test_catalog_keys_are_the_agreed_ones() -> None:
    from modules.bot.notify_catalog import CATALOG

    assert sorted(CATALOG) == sorted(ALL_KEYS)


def test_defaults_enable_everything_except_login_locked() -> None:
    from modules.bot.notify_catalog import default_events

    defaults = default_events()
    assert {key for key, on in defaults.items() if on} == set(ON_BY_DEFAULT)
    assert defaults[LOGIN_LOCKED] is False and defaults[PANEL_DOWN] is True


def test_audit_actions_for_the_new_stage_exist() -> None:
    from modules.audit.models.audit_log import AuditAction, AuditEntity

    names = {
        "server_health_changed",
        "bot_admin_link_code_created",
        "bot_admin_link_created",
        "bot_admin_link_removed",
        "bot_admin_link_events_updated",
        "bot_notify_test_queued",
    }
    assert names <= {action.value for action in AuditAction}
    assert AuditEntity.ADMIN_TELEGRAM_LINK == "admin_telegram_link"


def test_the_notify_permission_belongs_to_all_three_roles() -> None:
    from modules.auth.models.admin import AdminRole
    from modules.auth.permissions import Permission, has_permission

    assert Permission.BOT_NOTIFY.value == "bot:notify"
    assert all(has_permission(role, Permission.BOT_NOTIFY) for role in AdminRole)


# --- настройки ------------------------------------------------------------------------------------------------------


def make_settings(**fields: object) -> Settings:
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg,arg-type]


def test_notify_settings_have_the_agreed_defaults() -> None:
    settings = make_settings()
    assert settings.bot_notify_interval_seconds == 15
    assert settings.bot_notify_overlap_seconds == 2100
    assert settings.bot_notify_batch == 500
    assert settings.bot_notify_ttl_hours == 24
    assert settings.bot_notify_timezone == "UTC"
    assert settings.bot_admin_link_code_ttl_seconds == 600


def test_overlap_shorter_than_the_stale_job_timeout_is_refused() -> None:
    """Задача воркера коммитит позже `created_at` события: перекрытие меньше таймаута задач теряло бы события."""
    with pytest.raises(ValidationError):
        make_settings(stale_job_timeout_seconds=1800, bot_notify_overlap_seconds=1800)


def test_overlap_of_timeout_plus_five_minutes_is_accepted() -> None:
    assert make_settings(stale_job_timeout_seconds=1800, bot_notify_overlap_seconds=2100)


def test_invalid_notify_timezone_is_refused() -> None:
    with pytest.raises(ValidationError):
        make_settings(bot_notify_timezone="Invalid/Timezone")


def test_scheduler_has_the_bot_notify_rule() -> None:
    import inspect

    from infrastructure.workers import scheduler

    assert "bot_notify" in inspect.getsource(scheduler)
