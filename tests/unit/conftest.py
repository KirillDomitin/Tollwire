from pathlib import Path

import pytest

from tests.unit.install_support import prepare_app


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """Каталог установки во временной папке (см. install_support)."""
    return prepare_app(tmp_path)
