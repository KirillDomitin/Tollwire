"""Let's Encrypt в установщике и ./panel cert (этап 21.5).

certbot, sudo и apt-get — заглушки в PATH; хуки, которые установщик пишет для certbot, запускаются по-настоящему.
Заглушка certbot ведёт себя как настоящий: pre-hook → проверка → «выпуск» → deploy-hook → post-hook."""

import os
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from tests.unit.install_support import (
    BASH,
    ROOT,
    base_env,
    docker_calls,
    free_port,
    requires_bash,
    run,
    run_panel,
)

pytestmark = requires_bash

DOMAIN = "panel.example.org"
LE_FLAGS = ("--domain", DOMAIN, "--letsencrypt", "--accept-le-terms")


def calls(app: Path) -> list[str]:
    return docker_calls(app)


def certbot_calls(app: Path) -> list[str]:
    return [line for line in calls(app) if line.startswith("certbot ")]


def has(app: Path, needle: str) -> bool:
    return any(needle in line for line in calls(app))


def cert_issuer(app: Path) -> str:
    cert = x509.load_pem_x509_certificate((app / "certs" / "fullchain.pem").read_bytes())
    return cert.issuer.rfc4514_string()


def fingerprint(app: Path) -> bytes:
    return (app / "certs" / "fullchain.pem").read_bytes()


def issue(app: Path, *extra: str, **env: str) -> subprocess.CompletedProcess[str]:
    return run(app, *LE_FLAGS, *extra, **env)


# --- установщик: успешный выпуск ------------------------------------------------------------------------------------


def test_installer_issues_a_lets_encrypt_certificate(app: Path) -> None:
    result = issue(app)

    assert result.returncode == 0, result.stderr
    assert "Fake Let's Encrypt" in cert_issuer(app)  # самоподписанный заменён «выпущенным»
    assert "Сертификат Let's Encrypt установлен" in result.stdout
    dry, real = certbot_calls(app)
    assert "--dry-run" in dry and "--dry-run" not in real  # сначала проверка на тестовом сервере, потом выпуск
    assert f"-d {DOMAIN}" in real and "--standalone" in real and "--cert-name tollwire" in real


def test_new_certificate_matches_its_key_and_is_private(app: Path) -> None:
    issue(app)

    cert = x509.load_pem_x509_certificate((app / "certs" / "fullchain.pem").read_bytes())
    key = serialization.load_pem_private_key((app / "certs" / "privkey.pem").read_bytes(), password=None)
    assert key.public_key().public_numbers() == cert.public_key().public_numbers()  # type: ignore[union-attr]
    assert sorted(path.name for path in (app / "certs").iterdir()) == ["fullchain.pem", "privkey.pem"]  # без мусора
    if sys.platform != "win32":
        assert stat.S_IMODE((app / "certs" / "privkey.pem").stat().st_mode) == 0o600


def test_without_an_email_it_registers_without_one(app: Path) -> None:
    issue(app)

    real = certbot_calls(app)[-1]
    assert "--register-unsafely-without-email" in real and "--email" not in real


def test_email_is_passed_to_certbot(app: Path) -> None:
    issue(app, "--le-email", "me@example.org")

    real = certbot_calls(app)[-1]
    assert "--email me@example.org" in real and "--no-eff-email" in real
    assert "--register-unsafely-without-email" not in real


def test_hooks_are_installed_where_certbot_will_keep_using_them(app: Path) -> None:
    issue(app)

    hooks = app.parent / "le-hooks"
    assert sorted(path.name for path in hooks.iterdir()) == ["deploy.sh", "post.sh", "pre.sh"]
    real = certbot_calls(app)[-1]
    for name, flag in (("pre.sh", "--pre-hook"), ("post.sh", "--post-hook"), ("deploy.sh", "--deploy-hook")):
        assert f"{flag} {hooks}/{name}" in real  # хуки сохранятся в настройках обновления certbot
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        assert has(app, "sudo ")  # от обычного пользователя системные действия идут через sudo; от root sudo не нужен


def test_running_panel_gets_its_nginx_stopped_and_started_around_verification(app: Path) -> None:
    """Порт 80 нужен certbot: nginx панели на время проверки останавливается и потом возвращается — и для пробной
    проверки, и для настоящего выпуска."""
    issue(app, FAKE_RUNNING="1")

    log = calls(app)
    stops = [i for i, line in enumerate(log) if line.endswith("stop nginx")]
    starts = [i for i, line in enumerate(log) if line.endswith("start nginx")]
    certbots = [i for i, line in enumerate(log) if line.startswith("certbot ")]
    assert len(stops) == len(starts) == len(certbots) == 2
    for stop, start, certbot in zip(stops, starts, certbots, strict=True):
        assert certbot < stop < start  # заглушка certbot пишет вызов и тут же запускает pre-hook


def test_stopped_panel_is_left_stopped(app: Path) -> None:
    """Первая установка: nginx ещё не запущен — хуки его не трогают (и не запускают то, чего не останавливали)."""
    issue(app)

    assert not has(app, "stop nginx") and not has(app, "start nginx")


# --- установщик: отказ до записи и запасной путь --------------------------------------------------------------------


def test_terms_must_be_accepted_explicitly_without_a_terminal(app: Path) -> None:
    result = run(app, "--domain", DOMAIN, "--letsencrypt")

    assert result.returncode != 0 and "--accept-le-terms" in result.stderr
    assert not (app / ".env.prod").exists() and not (app / "certs").exists()  # отказ до записи секретов
    assert certbot_calls(app) == []


def test_bad_email_is_rejected_before_anything_is_written(app: Path) -> None:
    result = issue(app, "--le-email", "not-an-email")

    assert result.returncode != 0 and "email" in result.stderr
    assert not (app / ".env.prod").exists()


@pytest.mark.parametrize("domain", ["localhost", "203.0.113.5", "panel.local", "nohost"])
def test_domain_without_a_public_name_falls_back_to_self_signed(app: Path, domain: str) -> None:
    result = run(app, "--domain", domain, "--letsencrypt", "--accept-le-terms")

    assert result.returncode == 0, result.stderr  # панель всё равно получает сертификат и запускается
    assert "настоящий домен" in result.stderr and "самоподписанным" in result.stdout
    assert certbot_calls(app) == []


def test_failed_verification_leaves_the_self_signed_certificate(app: Path) -> None:
    result = issue(app, FAKE_CERTBOT_FAIL="1")

    assert result.returncode == 0, result.stderr
    assert "проверка домена не прошла" in result.stderr and "./panel cert letsencrypt" in result.stdout
    assert len(certbot_calls(app)) == 1 and "--dry-run" in certbot_calls(app)[0]  # настоящий выпуск не пробовали
    assert DOMAIN in cert_issuer(app) and "Fake" not in cert_issuer(app)


def test_failed_issuing_leaves_the_self_signed_certificate(app: Path) -> None:
    before = issue(app, FAKE_CERTBOT_FAIL="1", FAKE_CERTBOT_FAIL_REAL_ONLY="1")

    assert before.returncode == 0 and "выпуск сертификата не удался" in before.stderr
    assert "Fake" not in cert_issuer(app)


def test_certbot_that_changes_nothing_is_reported(app: Path) -> None:
    """certbot «отработал», но deploy-hook не положил файлы: это не успех, и пользователю так и скажут."""
    result = issue(app, FAKE_CERTBOT_NO_DEPLOY="1")

    assert "не заменился" in result.stderr and "Сертификат Let's Encrypt установлен" not in result.stdout


def test_busy_port_80_is_reported_before_certbot_runs(app: Path) -> None:
    port = free_port()
    with socket.socket() as server:
        server.bind(("127.0.0.1", port))
        server.listen()
        result = issue(app, TOLLWIRE_LE_HTTP_PORT=str(port))

    assert result.returncode == 0 and "порт 80 занят" in result.stderr
    assert certbot_calls(app) == []


def test_existing_certificate_is_not_replaced_by_the_installer(app: Path) -> None:
    run(app, "--domain", DOMAIN)  # первая установка: самоподписанный
    before = fingerprint(app)

    result = issue(app)

    assert result.returncode != 0 and "./panel cert letsencrypt" in result.stderr
    assert fingerprint(app) == before and certbot_calls(app) == []


# --- certbot: установка ---------------------------------------------------------------------------------------------


def test_missing_certbot_is_installed_first(app: Path) -> None:
    (app.parent / "bin" / "certbot").unlink()

    result = issue(app)

    assert result.returncode == 0, result.stderr
    log = calls(app)
    install = next(i for i, line in enumerate(log) if "apt-get" in line and "install" in line and "certbot" in line)
    first_certbot = next(i for i, line in enumerate(log) if line.startswith("certbot "))
    assert install < first_certbot and "Fake Let's Encrypt" in cert_issuer(app)


def test_certbot_is_not_reinstalled_when_present(app: Path) -> None:
    issue(app)
    assert not has(app, "apt-get")


def test_failed_certbot_installation_falls_back(app: Path) -> None:
    (app.parent / "bin" / "certbot").unlink()

    result = issue(app, FAKE_APT_FAIL="1")

    assert result.returncode == 0 and "не удалось установить certbot" in result.stderr
    assert certbot_calls(app) == [] and "Fake" not in cert_issuer(app)


# --- хуки -----------------------------------------------------------------------------------------------------------


def hook(app: Path, name: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BASH), str(app.parent / "le-hooks" / name)],
        env={**base_env(app), **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )


def test_pre_and_post_hooks_stop_and_restore_a_running_nginx(app: Path) -> None:
    issue(app)
    (app.parent / "docker.log").unlink()

    assert hook(app, "pre.sh", FAKE_RUNNING="1").returncode == 0
    assert (app.parent / "le-hooks" / "nginx-was-running").exists()
    assert hook(app, "post.sh").returncode == 0

    log = calls(app)
    assert any(line.endswith("stop nginx") for line in log) and any(line.endswith("start nginx") for line in log)
    assert not (app.parent / "le-hooks" / "nginx-was-running").exists()


def test_hooks_leave_a_stopped_panel_alone(app: Path) -> None:
    issue(app)
    (app.parent / "docker.log").unlink()

    hook(app, "pre.sh")  # nginx не запущен
    hook(app, "post.sh")

    assert not has(app, "stop nginx") and not has(app, "start nginx")


def test_deploy_hook_swaps_the_pair_into_certs(app: Path) -> None:
    issue(app)
    live = app.parent / "renewed"
    live.mkdir()
    fresh = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "5",
            "-subj",
            "/CN=renewed",
            "-keyout",
            str(live / "privkey.pem"),
            "-out",
            str(live / "fullchain.pem"),
        ],
        capture_output=True,
        env={**base_env(app), "MSYS_NO_PATHCONV": "1"},
    )
    assert fresh.returncode == 0

    result = hook(app, "deploy.sh", RENEWED_LINEAGE=str(live))

    assert result.returncode == 0, result.stderr
    assert (app / "certs" / "fullchain.pem").read_bytes() == (live / "fullchain.pem").read_bytes()
    assert (app / "certs" / "privkey.pem").read_bytes() == (live / "privkey.pem").read_bytes()
    assert sorted(path.name for path in (app / "certs").iterdir()) == ["fullchain.pem", "privkey.pem"]  # без .le.*


def test_deploy_hook_without_a_pair_changes_nothing(app: Path) -> None:
    issue(app)
    before = fingerprint(app)

    result = hook(app, "deploy.sh", RENEWED_LINEAGE=str(app.parent / "nowhere"))

    assert result.returncode != 0
    assert fingerprint(app) == before and sorted(p.name for p in (app / "certs").iterdir()) == [
        "fullchain.pem",
        "privkey.pem",
    ]


# --- ./panel cert ---------------------------------------------------------------------------------------------------


@pytest.fixture
def panel_app(app: Path) -> Path:
    """Установленная панель с самоподписанным сертификатом (как на сервере до перехода на Let's Encrypt)."""
    assert run(app, "--domain", DOMAIN).returncode == 0
    (app.parent / "docker.log").unlink(missing_ok=True)
    return app


def test_cert_status_describes_a_self_signed_certificate(panel_app: Path) -> None:
    result = run_panel(panel_app, "cert")

    assert result.returncode == 0, result.stderr
    assert "Действителен до:" in result.stdout and re.search(r"осталось дней: \d+", result.stdout)
    assert f"Покрывает {DOMAIN}:  да" in result.stdout
    assert "Let's Encrypt:    нет" in result.stdout and "./panel cert letsencrypt" in result.stdout


def test_cert_letsencrypt_switches_an_installed_panel(panel_app: Path) -> None:
    result = run_panel(panel_app, "cert", "letsencrypt", "--accept-le-terms", FAKE_RUNNING="1")

    assert result.returncode == 0, result.stderr
    assert "Fake Let's Encrypt" in cert_issuer(panel_app)
    status = run_panel(panel_app, "cert", "status")
    assert "Let's Encrypt:    да" in status.stdout
    assert has(panel_app, "stop nginx") and has(panel_app, "start nginx")  # живую панель на секунды останавливали


def test_cert_letsencrypt_needs_the_terms_without_a_terminal(panel_app: Path) -> None:
    before = fingerprint(panel_app)

    result = run_panel(panel_app, "cert", "letsencrypt")

    assert result.returncode != 0 and "--accept-le-terms" in result.stderr
    assert fingerprint(panel_app) == before and certbot_calls(panel_app) == []


def test_cert_letsencrypt_failure_keeps_the_working_certificate(panel_app: Path) -> None:
    before = fingerprint(panel_app)

    result = run_panel(panel_app, "cert", "letsencrypt", "--accept-le-terms", FAKE_CERTBOT_FAIL="1")

    assert result.returncode != 0 and "проверка домена не прошла" in result.stderr
    assert fingerprint(panel_app) == before


def test_cert_letsencrypt_refuses_a_domain_without_a_public_name(app: Path) -> None:
    run(app, "--domain", "localhost")

    result = run_panel(app, "cert", "letsencrypt", "--accept-le-terms")

    assert result.returncode != 0 and "настоящий домен" in result.stderr
    assert certbot_calls(app) == []


def test_cert_letsencrypt_passes_the_email(panel_app: Path) -> None:
    run_panel(panel_app, "cert", "letsencrypt", "--accept-le-terms", "--email", "me@example.org")
    assert "--email me@example.org" in certbot_calls(panel_app)[-1]


def test_cert_renew_runs_certbot_renew_for_our_certificate(panel_app: Path) -> None:
    result = run_panel(panel_app, "cert", "renew")

    assert result.returncode == 0, result.stderr
    assert certbot_calls(panel_app) == ["certbot renew --cert-name tollwire"]


def test_cert_renew_without_certbot_points_to_letsencrypt(panel_app: Path) -> None:
    (panel_app.parent / "bin" / "certbot").unlink()

    result = run_panel(panel_app, "cert", "renew")

    assert result.returncode != 0 and "./panel cert letsencrypt" in result.stderr


def test_cert_unknown_subcommand(panel_app: Path) -> None:
    result = run_panel(panel_app, "cert", "frobnicate")
    assert result.returncode != 0 and "frobnicate" in result.stderr


# --- общее ----------------------------------------------------------------------------------------------------------


def test_library_messages_exist_in_both_languages_with_matching_placeholders() -> None:
    text = (ROOT / "deploy" / "letsencrypt.sh").read_text(encoding="utf-8")

    def table(name: str) -> dict[str, str]:
        body = text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
        return dict(re.findall(r"^\s+(\w+)\) printf '%s' \"(.*)\" ;;$", body, re.M))

    ru, en = table("lib_msg_ru"), table("lib_msg_en")
    assert ru.keys() == en.keys() and len(ru) > 20
    assert {key: ru[key].count("%s") for key in ru} == {key: en[key].count("%s") for key in en}


def test_messages_follow_the_language(app: Path) -> None:
    english = run(app, "--domain", DOMAIN, "--letsencrypt", PANEL_LANG="en", LC_ALL="", LANG="")

    assert english.returncode != 0 and "needs explicit consent" in english.stderr
    assert not re.search("[А-Яа-я]", english.stderr)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("panel.example.org", True),
        ("a.b.c.example.com", True),
        ("localhost", False),
        ("203.0.113.5", False),
        ("panel.local", False),
        ("nohost", False),
        ("-bad.example.org", False),
        ("a..b.org", False),
        ("x.test", False),
    ],
)
def test_public_domain_check(name: str, expected: bool, tmp_path: Path) -> None:
    deploy = ROOT / "deploy"
    script = f'source "{deploy}/common.sh"; source "{deploy}/letsencrypt.sh"; le_public_domain {name!r}'
    result = subprocess.run([str(BASH), "-c", script], capture_output=True, text=True, encoding="utf-8")
    assert (result.returncode == 0) is expected
