"""Структурированные логи: формат JSON/text, контекст через bind, маскировка секретов, логи SSH-клиента."""

import json
import logging
from types import SimpleNamespace

import pytest

from core.log import JsonFormatter, TextFormatter, bind, current_context, redact, setup_logging
from infrastructure.ssh.client import SSHClient


@pytest.fixture(autouse=True)
def _logging() -> None:
    setup_logging("debug", "text")  # ставит фабрику записей, которая снимает контекст


def make_record(msg: str, **extra: object) -> logging.LogRecord:
    return logging.getLogger("test").makeRecord("test", logging.INFO, __file__, 1, msg, (), None, extra=extra)


def test_bind_nests_and_resets() -> None:
    with bind(request_id="r1"):
        with bind(job_id="j1", server_id=None):  # None не попадает в контекст
            assert current_context() == {"request_id": "r1", "job_id": "j1"}
        assert current_context() == {"request_id": "r1"}
    assert current_context() == {}


def test_json_line_has_context_and_extra_fields() -> None:
    with bind(request_id="r1", server_id="s1"):
        record = make_record("job started", duration_ms=12.5)
    entry = json.loads(JsonFormatter().format(record))
    assert entry["msg"] == "job started" and entry["level"] == "info" and entry["logger"] == "test"
    assert (entry["request_id"], entry["server_id"], entry["duration_ms"]) == ("r1", "s1", 12.5)
    assert entry["ts"].endswith("+00:00")  # время в UTC


def test_text_line_is_readable() -> None:
    with bind(job_id="j1"):
        line = TextFormatter().format(make_record("job failed", error="boom"))
    assert "INFO" in line and "test: job failed | job_id=j1 error=boom" in line


@pytest.mark.parametrize(
    "leak",
    [
        "PrivateKey = aGVsbG8gd29ybGQgaGVsbG8gd29ybGQgaGVsbG8gd29ybA=",
        "Authorization: Bearer abc.def-ghi",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
        "password=hunter2-secret",
        '{"password": "hunter2-secret"}',
        "refresh_token=abcdef123456",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----",
    ],
)
def test_redaction_safety_net(leak: str) -> None:
    for formatter in (JsonFormatter(), TextFormatter()):
        line = formatter.format(make_record(f"oops {leak}"))
        for secret in ("aGVsbG8", "abc.def", "c2lnbmF0dXJl", "hunter2", "abcdef123456", "AAAA"):
            assert secret not in line, line


def test_redaction_keeps_normal_text() -> None:
    text = "ssh command docker exec amnezia-awg awg show awg0 public-key exit_status=0"
    assert redact(text) == text


def test_setup_logging_is_idempotent() -> None:
    setup_logging("info", "json")
    setup_logging("info", "json")
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_vpn_panel", False)]
    assert len(ours) == 1 and isinstance(ours[0].formatter, JsonFormatter)


async def test_ssh_command_logged_without_output(caplog: pytest.LogCaptureFixture) -> None:
    class Conn:
        async def run(self, command: str, timeout: int) -> SimpleNamespace:
            return SimpleNamespace(stdout="PrivateKey = SECRET-OUTPUT", stderr="err-text", exit_status=0)

    client = SSHClient("203.0.113.10", password="pw")
    client._conn = Conn()  # type: ignore[assignment]
    caplog.set_level(logging.DEBUG, logger="ssh")
    with bind(job_id="j1", server_id="s1"):
        await client.execute("awg show awg0 dump")
    (record,) = [r for r in caplog.records if r.name == "ssh"]
    assert record.levelno == logging.DEBUG and record.command == "awg show awg0 dump"  # type: ignore[attr-defined]
    assert record.exit_status == 0 and record.context == {"job_id": "j1", "server_id": "s1"}  # type: ignore[attr-defined]
    dump = JsonFormatter().format(record)
    assert "SECRET-OUTPUT" not in dump and "err-text" not in dump and "pw" not in json.loads(dump).values()
