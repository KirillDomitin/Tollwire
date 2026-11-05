#!/usr/bin/env python3
"""Package committed deployment files with immutable image references (stdlib only)."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

PACKAGED_FILES = (
    "docker-compose.prod.yml",
    "deploy/nginx/default.conf",
    "scripts/deploy.sh",
    "scripts/rollback.sh",
    "scripts/backup.sh",
    "scripts/release.py",
)


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30
    ).stdout


def package(args: argparse.Namespace, root: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise ValueError("commit must be a full lowercase SHA")
    if git(root, "rev-parse", "HEAD").decode().strip() != args.commit:
        raise ValueError("commit must match the checked out HEAD")
    git(root, "diff", "--exit-code", "HEAD", "--", *PACKAGED_FILES)
    images = {"app": args.app, "web": args.web, "bot": args.bot}
    repositories = set()
    for role, image in images.items():
        pattern = r"(ghcr\.io/[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*)/" + role + r"@sha256:[0-9a-f]{64}"
        match = re.fullmatch(pattern, image)
        if not match:
            raise ValueError(f"invalid immutable {role} image")
        repositories.add(match.group(1))
    if len(repositories) != 1:
        raise ValueError("all images must belong to the same repository")
    if not args.schema_heads or any(not re.fullmatch(r"[A-Za-z0-9_]{1,64}", head) for head in args.schema_heads):
        raise ValueError("invalid Alembic schema heads")
    output = Path(args.output).absolute()
    if output.exists():
        raise ValueError("output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".release-", dir=output.parent))
    try:
        hashes = {}
        for name in PACKAGED_FILES:
            data = git(root, "show", f"{args.commit}:{name}")
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if name.endswith(".sh"):
                target.chmod(0o755)
            hashes[name] = hashlib.sha256(data).hexdigest()
        manifest = {
            "format_version": 1,
            "commit": args.commit,
            "platform": "linux/amd64",
            "images": images,
            "schema_heads": sorted(set(args.schema_heads)),
            "files": hashes,
        }
        (stage / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(f"Release {args.commit}: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--web", required=True)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--schema-heads", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        package(args, Path(__file__).resolve().parent.parent)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # No command output: git/Docker output can contain sensitive local details.
        parser.exit(1, f"Cannot package release: {exc if isinstance(exc, ValueError) else type(exc).__name__}\n")


if __name__ == "__main__":
    main()
