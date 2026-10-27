"""deploy/ship-images.sh: сборка образов из закоммиченного состояния и отправка на сервер по SSH.

Docker и ssh — заглушки в PATH (пишут вызовы в журнал); git, tar и gzip — настоящие."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.unit.install_support import BASH, ROOT, requires_bash

pytestmark = requires_bash

FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$SHIP_LOG"
case "$1" in
  build)
    ctx=${@: -1}
    echo "context-version: $(cat "$ctx/VERSION" 2>/dev/null)" >> "$SHIP_LOG"
    [ -z "${FAKE_BUILD_FAIL:-}" ] || exit 1
    ;;
  save) printf 'fake-image-archive' ;;
esac
"""

FAKE_SSH = """#!/usr/bin/env bash
echo "ssh $*" >> "$SHIP_LOG"
cat > /dev/null
[ -z "${FAKE_SSH_FAIL:-}" ] || exit 255
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Маленький git-репозиторий со скриптом и двумя Dockerfile; заглушки docker и ssh — в соседнем bin/."""
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "frontend").mkdir()
    for name in ("ship-images.sh", "common.sh"):
        shutil.copy(ROOT / "deploy" / name, repo / "deploy" / name)
    (repo / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repo / "frontend" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repo / "VERSION").write_text("committed", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("ssh", FAKE_SSH)):
        fake = bin_dir / name
        fake.write_bytes(body.encode())
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return repo


def ship(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PATH": f"{repo.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "SHIP_LOG": str(repo.parent / "ship.log"),
        "PANEL_LANG": "ru",
        **env,
    }
    return subprocess.run(
        [str(BASH), "deploy/ship-images.sh", *args],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def log(repo: Path) -> list[str]:
    path = repo.parent / "ship.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_builds_both_images_and_loads_them_on_the_server(repo: Path) -> None:
    result = ship(repo, "homelab")

    assert result.returncode == 0, result.stderr
    rev = git(repo, "rev-parse", "--short=12", "HEAD").strip()
    lines = log(repo)
    assert any(
        line.startswith("docker build") and line.endswith("/repo") is False and f"tollwire-ship/app:{rev}" in line
        for line in lines
    )
    assert any(f"tollwire-ship/web:{rev}" in line for line in lines)
    remote = next(line for line in lines if line.startswith("ssh homelab"))
    assert "docker load" in remote
    assert f"docker tag tollwire-ship/app:{rev} vpn-panel:latest" in remote
    assert f"docker tag tollwire-ship/web:{rev} vpn-panel-web:latest" in remote
    assert "Готово" in result.stdout and "--no-build" in result.stdout


def test_images_are_built_from_the_committed_state_only(repo: Path) -> None:
    (repo / "VERSION").write_text("edited and not committed", encoding="utf-8")

    result = ship(repo, "homelab")

    assert result.returncode == 0, result.stderr
    assert "context-version: committed" in log(repo)  # правка в рабочей папке в образ не попала
    assert "не попадут" in result.stdout  # а пользователю об этом сказано


def test_local_image_names_do_not_overwrite_the_panels_own_tags(repo: Path) -> None:
    ship(repo, "homelab")

    builds = [line for line in log(repo) if line.startswith("docker build")]
    assert builds and not any("vpn-panel:latest" in line or "vpn-panel-web:latest" in line for line in builds)


def test_failed_build_sends_nothing(repo: Path) -> None:
    result = ship(repo, "homelab", FAKE_BUILD_FAIL="1")

    assert result.returncode != 0 and "сборка образа не удалась" in result.stderr
    assert not any(line.startswith("ssh") for line in log(repo))


def test_failed_transfer_is_reported(repo: Path) -> None:
    result = ship(repo, "homelab", FAKE_SSH_FAIL="1")

    assert result.returncode != 0 and "не удалось передать образы на homelab" in result.stderr


@pytest.mark.parametrize("host", ["bad host", "a;rm -rf /", "-oProxyCommand=x", "$(id)", "", "a\nb"])
def test_server_name_is_checked_before_anything_runs(repo: Path, host: str) -> None:
    result = ship(repo, host)

    assert result.returncode != 0
    assert log(repo) == []  # ни сборки, ни ssh: имя уходит в команду ssh, поэтому проверяется строго


def test_needs_exactly_one_argument(repo: Path) -> None:
    assert ship(repo).returncode != 0 and ship(repo, "a", "b").returncode != 0
    assert log(repo) == []


@pytest.mark.parametrize("host", ["homelab", "admin@192.0.2.10", "srv-1.example.com"])
def test_usual_server_names_are_accepted(repo: Path, host: str) -> None:
    assert ship(repo, host).returncode == 0
    assert any(line.startswith(f"ssh {host} ") for line in log(repo))


def test_messages_follow_the_language(repo: Path) -> None:
    english = ship(repo, "homelab", PANEL_LANG="en")

    assert "Done:" in english.stdout and "Next, on the server" in english.stdout


# --- образ бота (этап 25.2) ------------------------------------------------------------------------------------------


def add_bot(repo: Path) -> None:
    (repo / "bot").mkdir()
    (repo / "bot" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add the bot")


def test_the_bot_image_is_shipped_as_the_third_one_when_the_repository_has_it(repo: Path) -> None:
    add_bot(repo)

    result = ship(repo, "homelab")

    assert result.returncode == 0, result.stderr
    rev = git(repo, "rev-parse", "--short=12", "HEAD").strip()
    lines = log(repo)
    assert any(line.startswith("docker build") and f"tollwire-ship/bot:{rev}" in line for line in lines)
    save = next(line for line in lines if line.startswith("docker save"))
    assert all(name in save for name in (f"app:{rev}", f"web:{rev}", f"bot:{rev}"))
    remote = next(line for line in lines if line.startswith("ssh homelab"))
    assert f"docker tag tollwire-ship/bot:{rev} vpn-panel-bot:latest" in remote
    assert "vpn-panel-bot:latest" in result.stdout


def test_without_a_bot_directory_only_two_images_are_shipped(repo: Path) -> None:
    result = ship(repo, "homelab")

    assert result.returncode == 0, result.stderr
    assert not any("tollwire-ship/bot" in line or "vpn-panel-bot" in line for line in log(repo))
    assert "vpn-panel-bot" not in result.stdout


def test_the_bot_image_does_not_overwrite_the_panels_own_tag(repo: Path) -> None:
    add_bot(repo)

    ship(repo, "homelab")

    builds = [line for line in log(repo) if line.startswith("docker build")]
    assert builds and not any("vpn-panel-bot:latest" in line for line in builds)


def test_a_failed_bot_build_sends_nothing(repo: Path) -> None:
    add_bot(repo)

    result = ship(repo, "homelab", FAKE_BUILD_FAIL="1")

    assert result.returncode != 0 and not any(line.startswith("ssh") for line in log(repo))
