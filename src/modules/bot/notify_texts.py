"""Plain notification rendering: never interpolate arbitrary audit metadata."""

import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from modules.bot.notify_catalog import CATALOG

_JOB_LABELS = {
    "server_install_failed": "установка сервера",
    "server_sync_failed": "синхронизация сервера",
    "server_discover_failed": "поиск VPN",
    "server_ssh_key_install_failed": "установка ключа SSH",
    "protocol_install_failed": "установка протокола",
    "protocol_remove_failed": "удаление протокола",
    "credential_issue_failed": "выдача доступа",
    "credential_revoke_failed": "отзыв доступа",
    "clients_import_failed": "импорт клиентов",
    "gateway_exit_add_failed": "добавление выхода шлюза",
    "gateway_exit_remove_failed": "удаление выхода шлюза",
    "gateway_ru_apply_failed": "применение маршрутов России",
}

_PRIVATE = re.compile(
    r"https?://\S+|\S+@\S+|(?:\d{1,3}\.){3}\d{1,3}|[\w.-]+\.[a-zA-Z]{2,}(?::\d+)?|"
    r"[0-9a-fA-F]*:[0-9a-fA-F:]+|\b\d{6,}\b|\\u[0-9a-fA-F]{4}"
)


def sanitize_name(value: str) -> str:
    clean = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
    return " ".join(_PRIVATE.sub("", clean).split())[:160]


def event_line(name: str, action: str, at: datetime, timezone: str, reason: object = None) -> str:
    stamp = at.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M %Z")
    detail = {"unreachable": "нет связи", "ssh_error": "ошибка SSH"}.get(reason) if isinstance(reason, str) else None
    label = "изменение состояния сервера" if action == "server_health_changed" else _JOB_LABELS.get(action)
    if label is None:
        label = next(
            (event.title for event in CATALOG.values() if any(a.value == action for a in event.actions)), "событие"
        )
    return f"{sanitize_name(name) or 'объект'} — {label}" + (f" ({detail})" if detail else "") + f" — {stamp}"


def render_notification(
    key: str,
    lines: Sequence[str],
    base_url: str | None,
    *,
    since: datetime,
    until: datetime,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
) -> str:
    title = CATALOG[key].title
    collapsed = len(lines) > 3
    body = [f"{title}: {len(lines)}" if collapsed else title, *(lines[:5] if collapsed else lines)]
    if collapsed and len(lines) > 5:
        body.append(f"и ещё {len(lines) - 5}, см. журнал")
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme in ("https", "http") and parsed.netloc and not parsed.username and not parsed.password:
            # AuditPage interprets dates in the viewer's local timezone. Pad the UTC
            # range so events remain visible for viewers on either side of UTC.
            filters = {
                "from": (since.astimezone(timezone.utc).date() - timedelta(days=1)).isoformat(),
                "to": (until.astimezone(timezone.utc).date() + timedelta(days=1)).isoformat(),
            }
            if entity_type and entity_id is not None:
                filters.update(entity_type=entity_type, entity_id=str(entity_id))
            body.append(base_url.rstrip("/") + "/audit?" + urlencode(filters))
    # Bound by Telegram's UTF-16 units, retaining whole lines.
    result: list[str] = []
    used = 0
    for line in body:
        size = len(line.encode("utf-16-le")) // 2 + bool(result)
        if used + size > 4096:
            break
        result.append(line)
        used += size
    return "\n".join(result)
