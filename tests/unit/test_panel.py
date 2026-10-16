"""./panel (этап 21.4): backup, restore, update, logs.

Скрипт запускается по-настоящему (bash) в копии каталога во временной папке; Docker — заглушка в PATH (она
«делает» pg_dump, принимает pg_restore и пишет все вызовы в журнал). git — настоящий, на локальном «сервере»."""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.unit.install_support import ROOT, docker_calls, installed, requires_bash, run_panel

pytestmark = requires_bash

PG_DUMP = "exec -T postgres sh -c pg_dump"
PG_RESTORE = "exec -T postgres sh -c pg_restore"
UP = "docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build"


def backups(app: Path) -> list[Path]:
    return sorted((app / "backups").glob("tollwire-[0-9]*.dump")) if (app / "backups").exists() else []


def position(calls: list[str], needle: str) -> int:
    return next(index for index, line in enumerate(calls) if needle in line)


def has(calls: list[str], needle: str) -> bool:
    return any(needle in line for line in calls)


@pytest.fixture
def app(app: Path) -> Path:
    return installed(app)


# --- backup -----------------------------------------------------------------------------------------------------------


def test_backup_writes_a_verified_private_dump(app: Path) -> None:
    result = run_panel(app, "backup", FAKE_DUMP_BODY="people and servers")

    assert result.returncode == 0, result.stderr
    (dump,) = backups(app)
    assert re.fullmatch(r"tollwire-\d{8}-\d{6}\.dump", dump.name)
    assert dump.read_bytes() == b"PGDMP-fake-dump:people and servers\n"
    assert f"backups/{dump.name}" in result.stdout  # путь показан относительным: так его проще скопировать
    assert sorted(path.name for path in (app / "backups").iterdir()) == [dump.name]  # без остатков .part и копий env


@pytest.mark.skipif(sys.platform == "win32", reason="права POSIX")
def test_backup_is_private(app: Path) -> None:
    run_panel(app, "backup")
    (dump,) = backups(app)
    assert dump.stat().st_mode & 0o777 == 0o600 and (app / "backups").stat().st_mode & 0o777 == 0o700


def test_backup_never_carries_the_encryption_key(app: Path) -> None:
    (app / ".env.prod").write_text("ENCRYPTION_KEY=precious-key-value\nPANEL_DOMAIN=localhost\n", encoding="utf-8")

    result = run_panel(app, "backup")

    (dump,) = backups(app)
    assert b"precious-key-value" not in dump.read_bytes()
    assert "ENCRYPTION_KEY" in result.stdout and "precious-key-value" not in result.stdout + result.stderr


def test_failed_pg_dump_leaves_nothing_behind(app: Path) -> None:
    result = run_panel(app, "backup", FAKE_DUMP_FAIL="1")

    assert result.returncode != 0 and "pg_dump" in result.stderr
    assert list((app / "backups").iterdir()) == []  # ни частичного файла, ни пустого дампа


def test_empty_dump_is_not_kept(app: Path) -> None:
    result = run_panel(app, "backup", FAKE_DUMP_EMPTY="1")

    assert result.returncode != 0
    assert list((app / "backups").iterdir()) == []


def test_backup_needs_a_running_database(app: Path) -> None:
    result = run_panel(app, "backup", FAKE_PG_DOWN="1")

    assert result.returncode != 0 and "контейнер базы не запущен" in result.stderr
    assert not has(docker_calls(app), PG_DUMP)


def test_old_backups_are_pruned_but_other_files_are_not(app: Path) -> None:
    folder = app / "backups"
    folder.mkdir()
    now = time.time()
    for index in range(1, 6):
        old = folder / f"tollwire-2026010{index}-000000.dump"
        old.write_bytes(b"PGDMP-old")
        os.utime(old, (now - 1000 * (10 - index), now - 1000 * (10 - index)))
    keepers = [folder / "notes.txt", folder / "tollwire-pre-restore-20260101-000000.dump"]
    for keeper in keepers:
        keeper.write_bytes(b"mine")
        os.utime(keeper, (now - 99999, now - 99999))  # самые старые: по возрасту их удалили бы первыми

    result = run_panel(app, "backup", "--keep", "3")

    assert result.returncode == 0, result.stderr
    kept = [path.name for path in backups(app)]
    assert len(kept) == 3 and "tollwire-20260101-000000.dump" not in kept  # остались три самых свежих
    assert all(keeper.exists() for keeper in keepers)  # страховочные и чужие файлы не трогаем
    assert "Старые бэкапы удалены: 3" in result.stdout


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5"])
def test_keep_must_be_a_positive_number(app: Path, value: str) -> None:
    result = run_panel(app, "backup", "--keep", value)

    assert result.returncode != 0 and "--keep" in result.stderr
    assert not has(docker_calls(app), PG_DUMP) and backups(app) == []


def test_backup_directory_can_be_chosen(app: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"

    result = run_panel(app, "backup", "--dir", str(target))

    assert result.returncode == 0, result.stderr
    assert len(list(target.glob("tollwire-*.dump"))) == 1 and not (app / "backups").exists()


def test_two_backups_in_one_second_do_not_overwrite_each_other(app: Path) -> None:
    """Время «останавливаем» подменой date: настоящие запуски почти никогда не попадают в одну секунду."""
    fake_date = app.parent / "bin" / "date"
    fake_date.write_bytes(b"#!/usr/bin/env bash\necho 20260101-000000\n")
    fake_date.chmod(0o755)

    for _ in range(3):
        assert run_panel(app, "backup").returncode == 0

    assert [path.name for path in backups(app)] == [
        "tollwire-20260101-000000-1.dump",
        "tollwire-20260101-000000-2.dump",
        "tollwire-20260101-000000.dump",
    ]


def test_panel_needs_an_installed_panel(app: Path) -> None:
    (app / ".env.prod").unlink()

    result = run_panel(app, "backup")

    assert result.returncode != 0 and "./install.sh" in result.stderr


# --- restore ----------------------------------------------------------------------------------------------------------


def make_dump(app: Path, body: bytes = b"PGDMP-fake-dump:saved\n") -> Path:
    path = app.parent / "saved.dump"
    path.write_bytes(body)
    return path


def test_restore_replaces_the_data_in_a_safe_order(app: Path, tmp_path: Path) -> None:
    dump = make_dump(app)
    sink = tmp_path / "restored.bin"

    result = run_panel(app, "restore", str(dump), "--yes", FAKE_RESTORE_SINK=str(sink))

    assert result.returncode == 0, result.stderr
    calls = docker_calls(app)
    steps = [PG_DUMP, "stop api worker", PG_RESTORE, "run --rm migrate", UP]
    assert [position(calls, step) for step in steps] == sorted(position(calls, step) for step in steps)
    assert sink.read_bytes() == dump.read_bytes()  # в pg_restore ушёл именно этот файл
    assert "--clean --if-exists --no-owner --single-transaction" in calls[position(calls, PG_RESTORE)]


def test_restore_takes_a_safety_backup_first(app: Path) -> None:
    result = run_panel(app, "restore", str(make_dump(app)), "--yes", FAKE_DUMP_BODY="before")

    (safety,) = list((app / "backups").glob("tollwire-pre-restore-*.dump"))
    assert safety.read_bytes() == b"PGDMP-fake-dump:before\n" and safety.name in result.stdout.replace("\\", "/")
    assert backups(app) == []  # обычные бэкапы и их ротация страховочный не задевает


def test_safety_backup_can_be_skipped(app: Path) -> None:
    run_panel(app, "restore", str(make_dump(app)), "--yes", "--no-safety-backup")

    assert not has(docker_calls(app), PG_DUMP)
    assert not (app / "backups").exists() or list((app / "backups").iterdir()) == []


def test_failed_safety_backup_stops_before_anything_is_touched(app: Path) -> None:
    result = run_panel(app, "restore", str(make_dump(app)), "--yes", FAKE_DUMP_FAIL="1")

    assert result.returncode != 0
    calls = docker_calls(app)
    assert not has(calls, "stop api worker") and not has(calls, PG_RESTORE)


def test_restore_without_confirmation_is_refused_when_there_is_no_terminal(app: Path) -> None:
    result = run_panel(app, "restore", str(make_dump(app)))

    assert result.returncode != 0 and "--yes" in result.stderr
    calls = docker_calls(app)
    assert not has(calls, "stop api worker") and not has(calls, PG_DUMP)


def test_file_that_is_not_a_dump_is_rejected_first(app: Path) -> None:
    junk = app.parent / "junk.dump"
    junk.write_bytes(b"this is not a dump")

    result = run_panel(app, "restore", str(junk), "--yes")

    assert result.returncode != 0 and "не дамп PostgreSQL" in result.stderr
    assert not has(docker_calls(app), "stop api worker")


def test_restore_needs_an_existing_file(app: Path) -> None:
    missing = run_panel(app, "restore", str(app.parent / "nope.dump"), "--yes")
    nothing = run_panel(app, "restore", "--yes")

    assert missing.returncode != 0 and "nope.dump" in missing.stderr
    assert nothing.returncode != 0 and "укажите файл" in nothing.stderr
    assert not has(docker_calls(app), "stop api worker")


def test_failed_restore_brings_the_services_back_and_skips_the_rest(app: Path) -> None:
    result = run_panel(app, "restore", str(make_dump(app)), "--yes", FAKE_RESTORE_FAIL="1")

    assert result.returncode != 0 and "восстановление не удалось" in result.stderr
    assert "tollwire-pre-restore-" in result.stderr  # сказано, где лежит страховочный бэкап
    calls = docker_calls(app)
    assert position(calls, "start api worker") > position(calls, PG_RESTORE)
    assert not has(calls, "run --rm migrate") and UP not in calls


def test_restore_needs_a_running_database(app: Path) -> None:
    result = run_panel(app, "restore", str(make_dump(app)), "--yes", FAKE_PG_DOWN="1")

    assert result.returncode != 0 and "контейнер базы не запущен" in result.stderr


# --- update -----------------------------------------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


def repo_with_origin(app: Path) -> Path:
    """Каталог установки как git-клон «сервера»; возвращает второй клон, из которого выходят новые версии."""
    root = app.parent
    origin = root / "origin.git"
    git(root, "init", "--bare", "-q", "-b", "main", str(origin))
    git(app, "init", "-q", "-b", "main")
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", "base")
    git(app, "remote", "add", "origin", str(origin))
    git(app, "push", "-q", "-u", "origin", "main")
    other = root / "other"
    git(root, "clone", "-q", str(origin), str(other))
    return other


def release(other: Path, name: str = "NEW_VERSION") -> None:
    (other / name).write_text("2", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", f"add {name}")
    git(other, "push", "-q")


def test_update_backs_up_pulls_and_restarts_in_order(app: Path) -> None:
    release(repo_with_origin(app))

    result = run_panel(app, "update")

    assert result.returncode == 0, result.stderr
    assert (app / "NEW_VERSION").exists()  # код подтянут
    assert len(backups(app)) == 1
    calls = docker_calls(app)
    assert position(calls, PG_DUMP) < position(calls, UP)
    assert "Обновление завершено" in result.stdout
    assert not has(calls, "scripts.create_admin")  # update не предлагает создавать админа


def test_update_without_backup_or_pull(app: Path) -> None:
    release(repo_with_origin(app))

    result = run_panel(app, "update", "--no-backup", "--no-pull")

    assert result.returncode == 0, result.stderr
    assert not (app / "NEW_VERSION").exists() and backups(app) == []
    assert UP in docker_calls(app) and not has(docker_calls(app), PG_DUMP)


def test_update_refuses_a_dirty_tree_before_doing_anything(app: Path) -> None:
    repo_with_origin(app)
    (app / "install.sh").write_text("# local edit\n", encoding="utf-8")  # отслеживаемый файл изменён

    result = run_panel(app, "update")

    assert result.returncode != 0 and "локальные изменения" in result.stderr
    assert backups(app) == [] and UP not in docker_calls(app)


def test_update_can_start_without_building(app: Path) -> None:
    result = run_panel(app, "update", "--no-pull", "--no-backup", "--no-build")

    assert result.returncode == 0, result.stderr
    calls = docker_calls(app)
    assert "docker compose --env-file .env.prod -f docker-compose.prod.yml up -d" in calls
    assert not has(calls, "--build")


def test_update_ignores_untracked_files(app: Path) -> None:
    repo_with_origin(app)
    (app / "scratch.txt").write_text("not tracked", encoding="utf-8")

    assert run_panel(app, "update", "--no-backup").returncode == 0


def test_update_outside_git_points_to_no_pull(app: Path) -> None:
    result = run_panel(app, "update")

    assert result.returncode != 0 and "--no-pull" in result.stderr
    assert backups(app) == [] and UP not in docker_calls(app)
    assert run_panel(app, "update", "--no-pull").returncode == 0  # а с --no-pull работает и без git


def test_update_stops_when_the_history_diverged(app: Path) -> None:
    other = repo_with_origin(app)
    release(other)
    (app / "local.txt").write_text("local", encoding="utf-8")
    git(app, "add", "local.txt")
    git(app, "commit", "-q", "-m", "local commit")

    result = run_panel(app, "update", "--no-backup")

    assert result.returncode != 0 and "git pull" in result.stderr
    assert UP not in docker_calls(app)


def test_failed_update_names_the_backup_and_the_way_back(app: Path) -> None:
    result = run_panel(app, "update", "--no-pull", FAKE_UP_FAIL="1")

    assert result.returncode != 0
    (dump,) = backups(app)
    assert dump.name in result.stderr.replace("\\", "/") and "./panel restore" in result.stderr


def test_failed_update_without_backup_says_so(app: Path) -> None:
    result = run_panel(app, "update", "--no-pull", "--no-backup", FAKE_UP_FAIL="1")

    assert result.returncode != 0 and "бэкап не делался" in result.stderr


def test_update_needs_a_running_database_for_the_backup(app: Path) -> None:
    result = run_panel(app, "update", "--no-pull", FAKE_PG_DOWN="1")

    assert result.returncode != 0 and "контейнер базы не запущен" in result.stderr
    assert UP not in docker_calls(app)
    assert run_panel(app, "update", "--no-pull", "--no-backup", FAKE_PG_DOWN="1").returncode == 0


# --- logs -------------------------------------------------------------------------------------------------------------


def test_logs_follow_the_last_hundred_lines_by_default(app: Path) -> None:
    result = run_panel(app, "logs")

    assert result.returncode == 0 and "log line" in result.stdout
    assert docker_calls(app)[-1].endswith("logs --tail 100 -f")


def test_logs_for_chosen_services_without_following(app: Path) -> None:
    run_panel(app, "logs", "api", "worker", "--tail", "5", "--no-follow")

    assert docker_calls(app)[-1].endswith("logs --tail 5 api worker")


@pytest.mark.parametrize("args", [["bogus"], ["api", "bogus"], ["--tail", "abc"], ["--tail", "-1"], ["--tail"]])
def test_bad_logs_arguments_are_rejected_before_docker(app: Path, args: list[str]) -> None:
    result = run_panel(app, "logs", *args)

    assert result.returncode != 0
    assert not has(docker_calls(app), "logs --tail")


# --- общее ------------------------------------------------------------------------------------------------------------


def test_help_and_unknown_commands(app: Path) -> None:
    for args in ([], ["--help"], ["help"]):
        result = run_panel(app, *args)
        assert result.returncode == 0 and "./panel КОМАНДА" in result.stdout

    unknown = run_panel(app, "frobnicate")
    assert unknown.returncode != 0 and "неизвестная команда: frobnicate" in unknown.stderr


def test_unknown_options_are_rejected(app: Path) -> None:
    for command in ("backup", "update", "logs"):
        result = run_panel(app, command, "--bogus")
        assert result.returncode != 0 and "--bogus" in result.stderr


NO_LOCALE = {"PANEL_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": ""}


def test_messages_are_english_by_default_and_follow_the_locale(app: Path) -> None:
    english = run_panel(app, "backup", "--keep", "0", **NO_LOCALE)
    russian = run_panel(app, "backup", "--keep", "0", **{**NO_LOCALE, "LANG": "ru_RU.UTF-8"})

    assert english.stderr.startswith("Error: --keep must be a whole number")
    assert russian.stderr.startswith("Ошибка: --keep")
    assert "Usage: ./panel" in run_panel(app, "--help", **NO_LOCALE).stdout


def test_both_languages_have_the_same_messages() -> None:
    text = (ROOT / "panel").read_text(encoding="utf-8")

    def table(name: str) -> dict[str, str]:
        body = text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
        return dict(re.findall(r"^\s+(\w+)\) printf '%s' \"(.*)\" ;;$", body, re.M))

    ru, en = table("msg_ru"), table("msg_en")
    assert ru.keys() == en.keys() and len(ru) > 30
    assert {key: ru[key].count("%s") for key in ru} == {key: en[key].count("%s") for key in en}


def test_script_survives_being_replaced_while_running() -> None:
    """update делает git pull, который может подменить сам ./panel. bash читает файл по мере выполнения, поэтому
    последняя строка обязана вызывать main и завершаться в той же строке — иначе он прочитал бы хвост нового файла."""
    lines = (ROOT / "panel").read_text(encoding="utf-8").rstrip("\n").splitlines()

    assert lines[-1] == 'main "$@"; exit $?'
    assert sum(line.startswith('main "$@"') for line in lines) == 1
    data = (ROOT / "panel").read_bytes()
    assert data.startswith(b"#!/usr/bin/env bash\n") and b"\r" not in data
