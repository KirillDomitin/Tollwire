"""Compact check output keeps full diagnostics in an isolated local log."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def check_script(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    target = scripts / "check.py"
    shutil.copyfile(ROOT / "scripts/check.py", target)
    return target


def run_check(script: Path, label: str, code: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), label, "--", sys.executable, "-c", code, *arguments],
        cwd=script.parent.parent,
        env={**os.environ, "CHECK_TEST_TOKEN": "fixture-environment-secret", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def log_path(result: subprocess.CompletedProcess[str]) -> Path:
    return Path(next(line.removeprefix("Log: ") for line in result.stdout.splitlines() if line.startswith("Log: ")))


def test_success_retains_large_output_and_prints_only_compact_result(check_script: Path) -> None:
    result = run_check(check_script, "backend-unit", "print('x' * 50000)", "fixture-argument-secret")

    assert result.returncode == 0, result.stderr
    assert "PASS: backend-unit" in result.stdout
    assert len(result.stdout.splitlines()) == 2
    assert len(result.stdout) < 1000
    assert result.stderr == ""
    assert log_path(result).read_text(encoding="utf-8") == "x" * 50000 + "\n"
    assert "fixture-argument-secret" not in result.stdout
    assert "fixture-environment-secret" not in result.stdout


def test_failure_propagates_exit_code_and_preserves_diagnostics_log(check_script: Path) -> None:
    result = run_check(check_script, "lint", "import sys; print('fixture diagnostic'); sys.exit(23)")

    assert result.returncode == 23
    assert "FAIL exit=23: lint" in result.stdout
    assert "fixture diagnostic" not in result.stdout
    assert log_path(result).read_text(encoding="utf-8") == "fixture diagnostic\n"


def test_invalid_label_never_executes_command_or_creates_log(check_script: Path) -> None:
    marker = check_script.parent.parent / "executed"
    result = run_check(check_script, "../unsafe-label", f"from pathlib import Path; Path({str(marker)!r}).touch()")

    assert result.returncode == 2
    assert not marker.exists()
    assert not (check_script.parent.parent / ".checks").exists()
