"""A release bundle describes committed runtime files and cannot carry local secrets."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FILES = (
    "docker-compose.prod.yml",
    "deploy/nginx/default.conf",
    "scripts/deploy.sh",
    "scripts/rollback.sh",
    "scripts/backup.sh",
    "scripts/release.py",
)
IMAGES = {role: f"ghcr.io/example/tollwire/{role}@sha256:{'b' * 64}" for role in ("app", "web", "bot")}


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    for name in (*RUNTIME_FILES, "scripts/package-release.py"):
        source = ROOT / name
        assert source.is_file(), f"release packaging is not implemented: {name}"
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    git(directory, "init", "-q", "-b", "master")
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", "base")
    return directory


def package(
    repo: Path,
    output: Path,
    *,
    commit: str | None = None,
    schema_heads: tuple[str, ...] = ("a9d1e6c42b70",),
    **images: str,
) -> subprocess.CompletedProcess[str]:
    selected = IMAGES | images
    return subprocess.run(
        [
            sys.executable,
            str(repo / "scripts/package-release.py"),
            "--commit",
            commit if commit is not None else git(repo, "rev-parse", "HEAD"),
            "--app",
            selected["app"],
            "--web",
            selected["web"],
            "--bot",
            selected["bot"],
            "--schema-heads",
            *schema_heads,
            "--output",
            str(output),
        ],
        cwd=repo,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def manifest(output: Path) -> dict[str, Any]:
    return json.loads((output / "release.json").read_text(encoding="utf-8"))


def test_package_records_exact_commit_images_and_runtime_hashes(repo: Path, tmp_path: Path) -> None:
    output = tmp_path / "release"

    result = package(repo, output)

    assert result.returncode == 0, result.stderr
    data = manifest(output)
    assert data["commit"] == git(repo, "rev-parse", "HEAD")
    assert data["images"] == IMAGES
    assert data["schema_heads"] == ["a9d1e6c42b70"]
    for name in RUNTIME_FILES:
        assert (output / name).read_bytes() == (repo / name).read_bytes()
        assert data["files"][name] == hashlib.sha256((output / name).read_bytes()).hexdigest()


def test_package_ignores_local_secrets_and_unrelated_untracked_files(repo: Path, tmp_path: Path) -> None:
    output = tmp_path / "release"
    # Synthetic files in a temporary fixture; no real deployment environment is read.
    (repo / ".env.prod").write_text("DO_NOT_PACKAGE=fixture-secret\n", encoding="utf-8")
    (repo / "HANDOFF.md").write_text("unrelated local work\n", encoding="utf-8")
    (repo / "certs").mkdir()
    (repo / "certs/privkey.pem").write_text("fixture-private-key\n", encoding="utf-8")

    result = package(repo, output)

    assert result.returncode == 0, result.stderr
    paths = {str(path.relative_to(output)).replace("\\", "/") for path in output.rglob("*") if path.is_file()}
    assert paths == {*RUNTIME_FILES, "release.json"}
    assert "fixture-secret" not in result.stdout + result.stderr
    assert "fixture-private-key" not in result.stdout + result.stderr


def test_package_rejects_sha_other_than_checked_out_source(repo: Path, tmp_path: Path) -> None:
    output = tmp_path / "release"

    result = package(repo, output, commit="f" * 40)

    assert result.returncode != 0
    assert not output.exists()


def test_package_rejects_modified_tracked_runtime_before_writing_bundle(repo: Path, tmp_path: Path) -> None:
    output = tmp_path / "release"
    with (repo / "deploy/nginx/default.conf").open("a", encoding="utf-8") as stream:
        stream.write("\n# uncommitted runtime change\n")

    result = package(repo, output)

    assert result.returncode != 0
    assert not output.exists()


def test_package_never_overwrites_existing_output(repo: Path, tmp_path: Path) -> None:
    output = tmp_path / "release"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("already published", encoding="utf-8")

    result = package(repo, output)

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "already published"
    assert not (output / "release.json").exists()


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/example/tollwire/app:latest",
        f"ghcr.io/another/tollwire/app@sha256:{'b' * 64}",
        "$(touch compromised)",
    ],
)
def test_package_rejects_unsafe_image_without_partial_output(repo: Path, tmp_path: Path, image: str) -> None:
    output = tmp_path / "release"

    result = package(repo, output, app=image)

    assert result.returncode != 0
    assert not output.exists()
    assert not (repo / "compromised").exists()


def test_package_rejects_schema_revision_longer_than_manifest_contract(repo: Path, tmp_path: Path) -> None:
    output = tmp_path / "release"

    result = package(repo, output, schema_heads=("a" * 65,))

    assert result.returncode != 0
    assert not output.exists()


def test_package_normalizes_duplicate_schema_heads_into_a_valid_manifest(repo: Path, tmp_path: Path) -> None:
    output = tmp_path / "release"

    result = package(repo, output, schema_heads=("a9d1e6c42b70", "a9d1e6c42b70"))

    assert result.returncode == 0, result.stderr
    assert manifest(output)["schema_heads"] == ["a9d1e6c42b70"]
