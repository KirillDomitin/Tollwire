"""Тесты сервисов и репозиториев: настоящая PostgreSQL (vpn_panel_test) + фейковый SSH-сервер."""

from collections.abc import AsyncIterator

import pytest


@pytest.fixture(autouse=True)
async def _clean(clean_db: None) -> AsyncIterator[None]:
    yield
