"""install.sh, запуск панели (этап 21.3): docker compose up, ожидание готовности, проверка HTTPS, администратор.

Docker и curl — заглушки; терминала у тестов нет, поэтому ветка «создать админа в терминале» проверяется отдельно
(живым прогоном на Linux с псевдотерминалом), а здесь — что без терминала установщик честно говорит, что делать."""

import re
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.unit.install_support import BASE_FILES, env_values, requires_bash, run

pytestmark = requires_bash

UP = "docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def listening(port: int) -> Iterator[None]:
    with socket.socket() as server:
        server.bind(("127.0.0.1", port))
        server.listen()
        yield


def start(app: Path, *args: str, **env: str):  # type: ignore[no-untyped-def]
    """Запуск с заведомо свободными портами: тест не должен зависеть от того, что слушает машина разработчика."""
    ports = {"HTTP_PORT": str(free_port()), "HTTPS_PORT": str(free_port())}
    return run(app, *args, start=True, **{**ports, **env})


def calls(app: Path) -> list[str]:
    log = app.parent / "docker.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


# --- запуск ---------------------------------------------------------------------------------------------------------


def test_panel_is_started_after_secrets_and_certificate_exist(app: Path) -> None:
    result = start(app, "--domain", "panel.example.com")

    assert result.returncode == 0, result.stderr
    log = calls(app)
    assert UP in log
    assert "UP env=1 cert=1" in log  # к моменту запуска всё готово: иначе compose упал бы на пустых секретах
    assert log.index(UP) < log.index("docker inspect -f {{.State.Health.Status}} api-id")  # ждём после запуска


def test_no_start_stops_before_docker_compose_up(app: Path) -> None:
    result = run(app, "--domain", "panel.example.com")  # по умолчанию в тестах start=False → --no-start

    assert result.returncode == 0 and UP not in calls(app)
    assert (app / ".env.prod").exists() and (app / "certs" / "fullchain.pem").exists()  # подготовка выполнена


def test_url_is_printed_with_the_configured_https_port(app: Path) -> None:
    https_port = free_port()
    result = start(app, "--domain", "panel.example.com", HTTPS_PORT=str(https_port))

    assert f"https://panel.example.com:{https_port}" in result.stdout


def test_default_https_port_is_not_shown_in_the_url(app: Path) -> None:
    result = start(app, "--domain", "panel.example.com", HTTPS_PORT="443")

    assert "https://panel.example.com" in result.stdout and "https://panel.example.com:" not in result.stdout


def test_ports_from_env_file_are_honoured(app: Path) -> None:
    https_port = free_port()
    (app / ".env.prod").write_text(f"PANEL_DOMAIN=panel.example.com\nHTTPS_PORT={https_port}\n", encoding="utf-8")

    result = run(app, start=True, HTTP_PORT=str(free_port()))

    assert f"https://panel.example.com:{https_port}" in result.stdout


def test_https_is_probed_through_nginx(app: Path) -> None:
    https_port = free_port()
    start(app, "--domain", "panel.example.com", HTTPS_PORT=str(https_port))

    probes = [line for line in calls(app) if line.startswith("curl")]
    assert probes and f"https://127.0.0.1:{https_port}/health" in probes[0]
    assert re.search(r"\s-[A-Za-z]*k", probes[0])  # -k: самоподписанный сертификат, цепочку проверить нечем


def test_rerun_is_safe_and_keeps_the_secrets(app: Path) -> None:
    first = start(app, "--domain", "panel.example.com")
    secrets = (env_values(app)["ENCRYPTION_KEY"], env_values(app)["POSTGRES_PASSWORD"])

    second = start(app)

    assert first.returncode == 0 and second.returncode == 0
    assert (env_values(app)["ENCRYPTION_KEY"], env_values(app)["POSTGRES_PASSWORD"]) == secrets
    assert calls(app).count(UP) == 2  # up -d идемпотентен: второй запуск просто приводит всё к рабочему виду


# --- сбои -----------------------------------------------------------------------------------------------------------


def test_failed_start_points_to_the_logs_and_does_not_wait(app: Path) -> None:
    result = start(app, "--domain", "panel.example.com", FAKE_UP_FAIL="1")

    assert result.returncode != 0 and "logs" in result.stderr
    assert not any("inspect" in line for line in calls(app))  # ждать нечего


def test_api_that_never_gets_healthy_fails_with_logs(app: Path) -> None:
    result = start(app, "--domain", "panel.example.com", FAKE_API_HEALTH="starting", INSTALL_WAIT_SECONDS="2")

    assert result.returncode != 0 and "api" in result.stderr
    assert any(line.endswith("logs --tail 30 api nginx") for line in calls(app))  # хвост логов показан сразу


def test_nginx_that_does_not_answer_over_https_fails(app: Path) -> None:
    result = start(app, "--domain", "panel.example.com", FAKE_CURL_FAIL="1", INSTALL_WAIT_SECONDS="2")

    assert result.returncode != 0 and "HTTPS" in result.stderr


@pytest.mark.parametrize("value", ["abc", "0", "-5", "1.5"])
def test_wait_time_must_be_a_positive_number(app: Path, value: str) -> None:
    result = start(app, "--domain", "panel.example.com", INSTALL_WAIT_SECONDS=value)

    assert result.returncode != 0 and "INSTALL_WAIT_SECONDS" in result.stderr
    assert UP not in calls(app)


def test_busy_port_is_reported_before_anything_starts(app: Path) -> None:
    port = free_port()
    with listening(port):
        result = start(app, "--domain", "panel.example.com", HTTPS_PORT=str(port))

    assert result.returncode != 0 and str(port) in result.stderr
    assert UP not in calls(app)


def test_busy_port_is_fine_when_it_is_our_own_running_panel(app: Path) -> None:
    """Повторный запуск при работающей панели: порты заняты нами самими."""
    port = free_port()
    with listening(port):
        result = start(app, "--domain", "panel.example.com", HTTPS_PORT=str(port), FAKE_RUNNING="1")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("port", ["0", "70000", "http", "-1"])
def test_nonsense_port_is_rejected(app: Path, port: str) -> None:
    result = start(app, "--domain", "panel.example.com", HTTPS_PORT=port)

    assert result.returncode != 0 and "HTTPS_PORT" in result.stderr
    assert UP not in calls(app)


def test_compose_file_is_required(app: Path) -> None:
    (app / "docker-compose.prod.yml").unlink()

    result = start(app, "--domain", "panel.example.com")

    assert result.returncode != 0 and "docker-compose.prod.yml" in result.stderr
    assert not (app / ".env.prod").exists()


# --- администратор --------------------------------------------------------------------------------------------------


def test_without_a_terminal_the_admin_step_is_explained_not_run(app: Path) -> None:
    result = start(app, "--domain", "panel.example.com")

    assert result.returncode == 0
    assert "scripts.create_admin --only-if-empty" in result.stdout  # дана готовая команда
    assert not any(" run " in f" {line} " for line in calls(app))  # сам установщик её без терминала не запускает


def test_admin_email_goes_into_the_suggested_command(app: Path) -> None:
    result = start(app, "--domain", "panel.example.com", "--admin-email", "boss@example.com")

    assert "scripts.create_admin --only-if-empty boss@example.com" in result.stdout


@pytest.mark.parametrize("email", ["no-at-sign", "a b@example.com", "@example.com", "a@", "x;y@example.com"])
def test_bad_admin_email_is_rejected_before_anything_is_written(app: Path, email: str) -> None:
    result = start(app, "--domain", "panel.example.com", "--admin-email", email)

    assert result.returncode != 0 and "email" in result.stderr.lower()
    assert {path.name for path in app.iterdir()} == BASE_FILES


def test_success_message_says_how_to_stop_and_read_logs(app: Path) -> None:
    result = start(app, "--domain", "panel.example.com")

    assert "logs" in result.stdout and "down" in result.stdout
    assert "самоподписанн" in result.stdout  # про предупреждение браузера напомнили и в конце


def test_no_admin_skips_the_admin_step_entirely(app: Path) -> None:
    result = start(app, "--domain", "panel.example.com", "--no-admin")

    assert result.returncode == 0, result.stderr
    assert "create_admin" not in result.stdout and not any("create_admin" in line for line in calls(app))
    assert "Панель запущена" in result.stdout
