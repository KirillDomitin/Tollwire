"""Ссылки уведомлений используют действующие фильтры журнала, включая локальные границы дней."""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest

from modules.bot.notify_texts import render_notification

SINCE = datetime(2026, 11, 1, 23, 59, tzinfo=timezone.utc)
UNTIL = datetime(2026, 11, 2, 0, 1, tzinfo=timezone.utc)
SERVER_ID = UUID("fbd45968-3e4e-4192-8a52-d1d3d71106fe")


def test_single_entity_link_filters_the_existing_audit_page() -> None:
    rendered = render_notification(
        "server_down",
        ["Север — нет связи"],
        "https://panel.example.test/",
        since=SINCE,
        until=SINCE,
        entity_type="server",
        entity_id=SERVER_ID,
    )

    link = urlsplit(rendered.splitlines()[-1])
    assert (link.scheme, link.netloc, link.path) == ("https", "panel.example.test", "/audit")
    assert parse_qs(link.query) == {
        "entity_type": ["server"],
        "entity_id": [str(SERVER_ID)],
        "from": ["2026-10-31"],
        "to": ["2026-11-02"],
    }


def test_multi_entity_link_covers_the_batch_without_selecting_one_entity() -> None:
    rendered = render_notification(
        "server_down",
        ["Север", "Юг", "Запад", "Восток"],
        "https://panel.example.test",
        since=SINCE,
        until=UNTIL,
    )

    assert parse_qs(urlsplit(rendered.splitlines()[-1]).query) == {
        "from": ["2026-10-31"],
        "to": ["2026-11-03"],
    }


@pytest.mark.parametrize("offset", [-12, 14])
def test_audit_day_filters_include_events_in_extreme_browser_timezones(offset: int) -> None:
    rendered = render_notification("server_down", ["Север"], "https://panel.example.test", since=SINCE, until=UNTIL)
    query = parse_qs(urlsplit(rendered.splitlines()[-1]).query)
    browser_zone = timezone(timedelta(hours=offset))
    # AuditPage.localDay converts dates at browser-local midnight; `to` is inclusive.
    lower = datetime.fromisoformat(query["from"][0]).replace(tzinfo=browser_zone)
    upper = datetime.fromisoformat(query["to"][0]).replace(tzinfo=browser_zone) + timedelta(days=1)

    assert lower <= SINCE <= UNTIL < upper


@pytest.mark.parametrize(
    "base_url", [None, "", "javascript:alert(1)", "https://secret@panel.example.test", "relative/path"]
)
def test_invalid_panel_url_does_not_add_a_link(base_url: str | None) -> None:
    rendered = render_notification("server_down", ["Север"], base_url, since=SINCE, until=UNTIL)

    assert rendered.splitlines()[-1] == "Север"
    assert "/audit" not in rendered


def test_notification_stays_within_telegram_utf16_limit() -> None:
    lines = ["😀" * 500 for _ in range(8)]
    rendered = render_notification("server_down", lines, "https://panel.example.test", since=SINCE, until=UNTIL)

    assert 0 < len(rendered.encode("utf-16-le")) // 2 <= 4096
    assert all(line == lines[0] for line in rendered.splitlines()[1:])
