"""Изоляция бота (этап 25.2): каталог `bot/` не импортирует код панели - только HTTP-клиент к Bot API.

Правило как «`infrastructure/` не импортирует `modules/`»: проверяется обходом AST всех `bot/**/*.py` (код и тесты).
Бот собирается в свой образ без `src/`; импорт панели работал бы на машине разработчика и падал бы в контейнере."""

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BOT = ROOT / "bot"
PACKAGE = BOT / "src" / "tollwire_bot"

# Верхний уровень пакетов панели: core, modules, infrastructure, scripts - каталоги src/; main и worker - её точки
# входа; src - сам каталог (from src.core import …).
PANEL_PACKAGES = {"core", "modules", "infrastructure", "scripts", "main", "worker", "src"}
PANEL_STACK = {
    "sqlalchemy",
    "alembic",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "redis",
    "fastapi",
    "uvicorn",
    "starlette",
    "asyncssh",
    "argon2-cffi",
    "pyjwt",
    "cryptography",
}
SKIPPED_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "build",
    "dist",
}
MODULES = (
    "config",
    "panel",
    "texts",
    "validation",
    "storage",
    "dialog",
    "commands",
    "watcher",
    "heartbeat",
    "log",
    "app",
)


def bot_sources() -> list[Path]:
    return sorted(
        path
        for path in BOT.rglob("*.py")
        if not SKIPPED_DIRS & set(path.relative_to(BOT).parts) and "site-packages" not in path.parts
    )


def imported_roots(source: str) -> list[tuple[int, str]]:
    """(строка, верхний уровень) каждого импорта: import/from и динамические import_module/__import__/find_spec."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name.split(".")[0]) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module.split(".")[0]))
        elif isinstance(node, ast.Call):
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            first = node.args[0] if node.args else None
            dynamic = callee in {"import_module", "__import__", "find_spec"}
            if dynamic and isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((node.lineno, first.value.split(".")[0]))
    return found


# --- сам детектор ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import core.config", {"core"}),
        ("from modules.bot.keys import BotKeyService", {"modules"}),
        ("from infrastructure.workers import worker", {"infrastructure"}),
        ("from scripts.bot_key import run", {"scripts"}),
        ("import main", {"main"}),
        ("from src.core import security", {"src"}),
        ("import importlib\nimportlib.import_module('modules.bot')", {"modules", "importlib"}),
        ("__import__('worker')", {"worker"}),
        ("import httpx, ssl\nfrom . import __version__\nfrom .panel import PanelClient", {"httpx", "ssl"}),
        ("from tollwire_bot.core import thing", {"tollwire_bot"}),
    ],
)
def test_the_detector_sees_every_form_of_import(source: str, expected: set[str]) -> None:
    assert {name for _, name in imported_roots(source)} == expected


# --- сам бот ---------------------------------------------------------------------------------------------------------


def test_the_bot_has_code_to_check() -> None:
    assert (PACKAGE / "__init__.py").is_file()
    assert len(bot_sources()) >= 5


@pytest.mark.parametrize("module", [*MODULES, "__main__"])
def test_the_package_has_the_modules_of_the_plan(module: str) -> None:
    assert (PACKAGE / f"{module}.py").is_file(), f"bot/src/tollwire_bot/{module}.py"


def test_the_bot_does_not_import_the_panel() -> None:
    violations = []
    for path in bot_sources():
        for line, name in imported_roots(path.read_text(encoding="utf-8")):
            if name in PANEL_PACKAGES:
                violations.append(f"{path.relative_to(ROOT).as_posix()}:{line} imports {name}")
    assert not violations, "\n".join(violations)


def test_the_bot_does_not_reach_into_the_panel_through_sys_path() -> None:
    """Подмена sys.path вернула бы запрещённый импорт через заднюю дверь (в тестах бота pythonpath задаёт pytest)."""
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            text = ast.unparse(node) if isinstance(node, ast.Call) else ""
            if text.startswith(("sys.path.insert", "sys.path.append", "sys.path.extend")):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert not offenders, "\n".join(offenders)


def test_the_panel_client_needs_only_httpx_certifi_and_the_standard_library() -> None:
    """Этим клиентом test_bot_client_contract.py проверяет настоящую панель в окружении, где aiogram нет."""
    allowed = set(sys.stdlib_module_names) | {"httpx", "certifi"}
    foreign = sorted(
        {name for _, name in imported_roots((PACKAGE / "panel.py").read_text(encoding="utf-8"))}
        - allowed
        - {"tollwire_bot"}
    )
    assert not foreign, f"panel.py imports {foreign}"


def test_the_bot_does_not_depend_on_the_panel_stack() -> None:
    project = tomllib.loads((BOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = {re.split(r"[\s<>=!~\[;]", spec, maxsplit=1)[0].lower() for spec in project["project"]["dependencies"]}
    assert not names & PANEL_STACK, sorted(names & PANEL_STACK)
    assert "aiogram" in names and "httpx" in names


def test_the_panel_image_does_not_carry_the_bot() -> None:
    lines = {line.strip().rstrip("/") for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    assert "bot" in lines  # образ панели собирается из корня: код бота и его тесты туда не попадают


@pytest.mark.parametrize(
    "name", ["Dockerfile", "docker-compose.yml", "install-bot.sh", ".env.example", ".dockerignore"]
)
def test_the_bot_directory_is_self_contained(name: str) -> None:
    assert (BOT / name).is_file(), f"bot/{name}"
