"""install.sh, TLS-сертификат (этап 21.2): самоподписанный по умолчанию, свои файлы через --cert/--key, существующие
файлы не затираются, негодные отвергаются. Окружение и заглушка Docker — из test_install_script.py."""

import datetime
import stat
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.unit.install_support import requires_bash, run

pytestmark = requires_bash

PEM_KEY_MARK = "PRIVATE KEY"


def make_pair(
    domain: str = "panel.example.com", *, days_left: int = 365, started_days_ago: int = 1
) -> tuple[bytes, bytes]:
    """Сертификат и ключ «как от настоящего центра»: для проверки режима «свои файлы»."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=started_days_ago))
        .not_valid_after(now + datetime.timedelta(days=days_left))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert.public_bytes(serialization.Encoding.PEM), pem_key


def load_cert(app: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate((app / "certs" / "fullchain.pem").read_bytes())


def san_values(cert: x509.Certificate) -> tuple[list[str], list[str]]:
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return san.get_values_for_type(x509.DNSName), [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]


def place_certs(app: Path, cert: bytes, key: bytes) -> None:
    (app / "certs").mkdir(exist_ok=True)
    (app / "certs" / "fullchain.pem").write_bytes(cert)
    (app / "certs" / "privkey.pem").write_bytes(key)


def write_own(tmp_path: Path, cert: bytes, key: bytes) -> tuple[Path, Path]:
    cert_path, key_path = tmp_path / "my.crt", tmp_path / "my.key"
    cert_path.write_bytes(cert)
    key_path.write_bytes(key)
    return cert_path, key_path


# --- самоподписанный по умолчанию -----------------------------------------------------------------------------------


def test_self_signed_certificate_is_issued_by_default(app: Path) -> None:
    result = run(app, "--domain", "panel.example.com")

    assert result.returncode == 0, result.stderr
    cert = load_cert(app)
    key = serialization.load_pem_private_key((app / "certs" / "privkey.pem").read_bytes(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.public_key().public_numbers() == cert.public_key().public_numbers()  # пара подходит друг другу
    assert cert.subject.rfc4514_string() == "CN=panel.example.com"
    assert san_values(cert) == (["panel.example.com"], [])  # браузеры сверяют SAN, а не CN
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert 800 <= lifetime.days <= 830
    assert sorted(path.name for path in (app / "certs").iterdir()) == ["fullchain.pem", "privkey.pem"]  # без мусора
    assert "самоподписанный" in result.stdout


def test_ip_address_gets_an_ip_san(app: Path) -> None:
    run(app, "--domain", "203.0.113.5")
    assert san_values(load_cert(app)) == ([], ["203.0.113.5"])


def test_localhost_certificate_covers_loopback(app: Path) -> None:
    run(app)
    assert san_values(load_cert(app)) == (["localhost"], ["127.0.0.1"])


@pytest.mark.skipif(sys.platform == "win32", reason="права POSIX")
def test_private_key_is_private(app: Path) -> None:
    run(app)
    assert stat.S_IMODE((app / "certs" / "privkey.pem").stat().st_mode) == 0o600


def test_private_key_is_never_printed(app: Path) -> None:
    result = run(app)
    assert PEM_KEY_MARK not in result.stdout + result.stderr


# --- существующие файлы ---------------------------------------------------------------------------------------------


def test_existing_certificate_pair_is_never_replaced(app: Path) -> None:
    """Боевой сертификат (например, от центра сертификации) нельзя затереть самоподписанным."""
    cert, key = make_pair("real.example.org")
    place_certs(app, cert, key)

    result = run(app, "--domain", "panel.example.com")

    assert result.returncode == 0, result.stderr
    assert (app / "certs" / "fullchain.pem").read_bytes() == cert
    assert (app / "certs" / "privkey.pem").read_bytes() == key


def test_certificate_for_existing_env_uses_its_domain(app: Path) -> None:
    line = "PANEL_DOMAIN=from-env.example.com   # комментарий\n"
    (app / ".env.prod").write_text(line, encoding="utf-8")

    result = run(app)  # .env.prod есть, сертификата нет: повторный запуск доделывает недостающее

    assert result.returncode == 0, result.stderr
    assert san_values(load_cert(app)) == (["from-env.example.com"], [])
    assert (app / ".env.prod").read_text(encoding="utf-8") == line  # .env.prod по-прежнему не тронут


@pytest.mark.parametrize("missing", ["fullchain.pem", "privkey.pem"])
def test_half_a_pair_stops_the_install_and_keeps_the_rest(app: Path, missing: str) -> None:
    place_certs(app, *make_pair())
    (app / "certs" / missing).unlink()
    (left,) = (app / "certs").iterdir()
    before = left.read_bytes()

    result = run(app)

    assert result.returncode != 0 and missing in result.stderr
    assert left.read_bytes() == before and [p.name for p in (app / "certs").iterdir()] == [left.name]


def test_mismatched_pair_is_rejected(app: Path) -> None:
    cert, _ = make_pair()
    _, other_key = make_pair()
    place_certs(app, cert, other_key)

    result = run(app)

    assert result.returncode != 0 and "не подходит" in result.stderr


def test_expired_certificate_is_rejected(app: Path) -> None:
    place_certs(app, *make_pair(days_left=-1, started_days_ago=30))

    result = run(app)

    assert result.returncode != 0 and "истёк" in result.stderr


def test_garbage_in_certs_is_rejected(app: Path) -> None:
    place_certs(app, b"not a certificate", b"not a key")

    result = run(app)

    assert result.returncode != 0 and "fullchain.pem" in result.stderr


def test_soon_expiring_certificate_is_a_warning_not_a_failure(app: Path) -> None:
    place_certs(app, *make_pair(days_left=10))

    result = run(app)

    assert result.returncode == 0 and "скоро истекает" in result.stdout


def test_certificate_for_another_domain_is_a_warning(app: Path) -> None:
    place_certs(app, *make_pair("other.example.org"))

    result = run(app, "--domain", "panel.example.com")

    assert result.returncode == 0 and "не покрывает" in result.stdout


# --- свои файлы: --cert / --key -------------------------------------------------------------------------------------


def test_own_certificate_is_copied_into_place(app: Path, tmp_path: Path) -> None:
    cert, key = make_pair("panel.example.com")
    cert_path, key_path = write_own(tmp_path, cert, key)

    result = run(app, "--domain", "panel.example.com", "--cert", str(cert_path), "--key", str(key_path))

    assert result.returncode == 0, result.stderr
    assert (app / "certs" / "fullchain.pem").read_bytes() == cert
    assert (app / "certs" / "privkey.pem").read_bytes() == key
    assert sorted(path.name for path in (app / "certs").iterdir()) == ["fullchain.pem", "privkey.pem"]
    assert PEM_KEY_MARK not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="права POSIX")
def test_own_key_is_made_private(app: Path, tmp_path: Path) -> None:
    cert_path, key_path = write_own(tmp_path, *make_pair("localhost"))
    key_path.chmod(0o644)

    run(app, "--cert", str(cert_path), "--key", str(key_path))

    assert stat.S_IMODE((app / "certs" / "privkey.pem").stat().st_mode) == 0o600


@pytest.mark.parametrize("only", ["--cert", "--key"])
def test_cert_and_key_come_together(app: Path, tmp_path: Path, only: str) -> None:
    cert_path, key_path = write_own(tmp_path, *make_pair())

    result = run(app, only, str(cert_path if only == "--cert" else key_path))

    assert result.returncode != 0 and "--cert" in result.stderr and "--key" in result.stderr
    assert not (app / ".env.prod").exists()


def test_bad_own_certificate_stops_before_anything_is_written(app: Path, tmp_path: Path) -> None:
    """Ошибка в сертификате не должна оставлять .env.prod с уже сгенерированными секретами."""
    cert, _ = make_pair()
    _, other_key = make_pair()
    cert_path, key_path = write_own(tmp_path, cert, other_key)

    result = run(app, "--cert", str(cert_path), "--key", str(key_path))

    assert result.returncode != 0
    assert sorted(path.name for path in app.iterdir()) == [".env.prod.example", "install.sh"]


def test_missing_own_files_are_reported(app: Path, tmp_path: Path) -> None:
    result = run(app, "--cert", str(tmp_path / "nope.crt"), "--key", str(tmp_path / "nope.key"))

    assert result.returncode != 0 and "nope.crt" in result.stderr


def test_own_certificate_does_not_replace_an_existing_one(app: Path, tmp_path: Path) -> None:
    existing = make_pair("old.example.org")
    place_certs(app, *existing)
    cert_path, key_path = write_own(tmp_path, *make_pair("new.example.org"))

    result = run(app, "--cert", str(cert_path), "--key", str(key_path))

    assert result.returncode != 0 and "certs" in result.stderr
    assert (app / "certs" / "fullchain.pem").read_bytes() == existing[0]


def test_bad_domain_issues_no_certificate(app: Path) -> None:
    run(app, "--domain", "bad domain")
    assert not (app / "certs").exists()


def test_garbage_key_with_a_good_certificate_names_the_key_file(app: Path) -> None:
    cert, _ = make_pair()
    place_certs(app, cert, b"not a key")

    result = run(app)

    assert result.returncode != 0 and "privkey.pem" in result.stderr and "закрытый ключ" in result.stderr


def test_key_with_a_passphrase_is_refused_without_waiting_for_input(app: Path) -> None:
    cert, key = make_pair()
    plain = serialization.load_pem_private_key(key, password=None)
    locked = plain.private_bytes(  # type: ignore[union-attr]
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b"secret")
    )
    place_certs(app, cert, locked)

    result = run(app)  # stdin закрыт, таймаут запуска — минута: зависание превратилось бы в падение теста

    assert result.returncode != 0 and "закрытый ключ" in result.stderr
