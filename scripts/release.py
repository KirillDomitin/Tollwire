"""Deploy trusted release bundles without rebuilding images or restoring database data.

Python 3 and Linux flock are host requirements. Only fixed subprocess argument lists
are used; neither manifest values nor deployment state are executed as shell code.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator


class ReleaseError(RuntimeError):
    """A deployment cannot safely proceed."""


FILES = ("docker-compose.prod.yml", "deploy/nginx/default.conf")
ALLOWED_FILES = (*FILES, "scripts/deploy.sh", "scripts/rollback.sh", "scripts/backup.sh", "scripts/release.py")
ROLES = ("app", "web", "bot")
REVISION = re.compile(r"[a-zA-Z0-9_]{1,64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"(ghcr\.io/[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*)/(app|web|bot)@sha256:([0-9a-f]{64})\Z")
MANAGEMENT_GUARD = b"""# tollwire-release-guard-v1
case "${1:-}" in
    update|restore|bot)
        if [ -f "$(dirname -- "${BASH_SOURCE[0]}")/.deploy/current.json" ]; then
            printf '%s\\n' 'Release-managed panel: legacy update/restore/bot disabled; use release tools.' >&2
            exit 1
        fi
        ;;
esac
# end tollwire-release-guard-v1
"""


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError("Duplicate JSON key")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > 100_000:
            raise ReleaseError("Unsafe or oversized JSON file")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, ValueError) as exc:
        raise ReleaseError("Cannot read deployment JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseError("Deployment JSON must be an object")
    return value


def valid_revisions(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and REVISION.fullmatch(item) for item in value)
        and len(value) == len(set(value))
    )


def validate_manifest(bundle: Path) -> dict[str, Any]:
    """Validate the release format and local checksums, not its publisher's identity."""
    bundle = bundle.resolve()
    manifest = read_json(bundle / "release.json")
    if set(manifest) != {"format_version", "commit", "platform", "images", "schema_heads", "files"}:
        raise ReleaseError("Invalid manifest fields")
    if type(manifest["format_version"]) is not int or manifest["format_version"] != 1:
        raise ReleaseError("Unsupported manifest format")
    if not isinstance(manifest["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]):
        raise ReleaseError("Invalid release commit")
    if manifest["platform"] != "linux/amd64" or not valid_revisions(manifest["schema_heads"]):
        raise ReleaseError("Unsupported platform or schema heads")
    images = manifest["images"]
    if not isinstance(images, dict) or set(images) != set(ROLES):
        raise ReleaseError("Manifest must pin app, web and bot images")
    repositories = set()
    for role in ROLES:
        match = IMAGE.fullmatch(images[role]) if isinstance(images[role], str) else None
        if not match or match[2] != role:
            raise ReleaseError("Invalid pinned image reference")
        repositories.add(match[1])
    if len(repositories) != 1:
        raise ReleaseError("Image repositories must match")
    checksums = manifest["files"]
    if not isinstance(checksums, dict) or not set(FILES) <= set(checksums) <= set(ALLOWED_FILES):
        raise ReleaseError("Manifest file list does not match release files")
    for name in checksums:
        path = bundle / name
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != bundle.parent):
            raise ReleaseError("Symlink in release file path")
        digest = checksums[name]
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ReleaseError("Invalid file checksum")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ReleaseError("Missing release file") from exc
        if actual != digest:
            raise ReleaseError("Release file checksum mismatch")
    return manifest


class Deployment:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.state = self.root / ".deploy"
        self.migration_uncertain = False
        self.migration_container: str | None = None

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        if sys.platform != "linux":
            raise ReleaseError("Release deployment requires Linux and Python 3")
        import fcntl

        if self.state.is_symlink():
            raise ReleaseError("Deployment state cannot be a symlink")
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.state.chmod(0o700)
        lock_path = self.state / "lock"
        if lock_path.is_symlink():
            raise ReleaseError("Deployment lock cannot be a symlink")
        with lock_path.open("a") as handle:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReleaseError("Another deployment is running") from exc
            yield

    def write_state(self, name: str, value: dict[str, Any]) -> None:
        self.state.mkdir(mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=self.state)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def install_management_guard(self) -> None:
        """Protect the existing panel before creating managed release state.

        Preserve its original body and libraries: copying a newer full panel onto
        an older installation could introduce unrelated dependency changes.
        """
        panel = self.root / "panel"
        temporary: str | None = None
        try:
            if panel.is_symlink() or not panel.is_file() or panel.stat().st_size > 1_000_000:
                raise ReleaseError("Existing panel must be a regular, non-symlink script")
            original = panel.read_bytes()
            shebang, separator, body = original.partition(b"\n")
            if not separator or not shebang.startswith(b"#!"):
                raise ReleaseError("Existing panel has no valid shebang")
            guarded = shebang + b"\n" + MANAGEMENT_GUARD
            if original.startswith(guarded):
                return
            if b"tollwire-release-guard-v1" in original:
                raise ReleaseError("Existing release guard differs from the expected guard")
            backup = self.state / "legacy-panel"
            if backup.exists() or backup.is_symlink():
                if backup.is_symlink() or not backup.is_file() or backup.read_bytes() != original:
                    raise ReleaseError("Legacy panel backup differs; reconcile it before deployment")
            else:
                with backup.open("xb") as handle:
                    os.chmod(backup, 0o600)
                    handle.write(original)
                    handle.flush()
                    os.fsync(handle.fileno())
            descriptor, temporary = tempfile.mkstemp(prefix=".panel-guard-", dir=self.root)
            with os.fdopen(descriptor, "wb") as handle:
                os.chmod(temporary, panel.stat().st_mode & 0o7777)
                handle.write(guarded + body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, panel)
            temporary = None
        except OSError as exc:
            raise ReleaseError("Cannot install legacy panel management guard") from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def run(
        self,
        args: list[str],
        env: dict[str, str] | None = None,
        input: bytes | None = None,
        timeout: int = 120,
    ) -> str:
        try:
            result = subprocess.run(
                args,
                cwd=self.root,
                env=env,
                input=input,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseError("Deployment command unavailable or timed out") from exc
        if result.returncode:
            # Child output can contain credentials or configuration; never echo it.
            raise ReleaseError("Deployment command failed; inspect the failed stage locally")
        return result.stdout.decode("utf-8").strip()

    def compose(
        self,
        record: dict[str, Any],
        args: list[str],
        input: bytes | None = None,
        timeout: int = 120,
    ) -> str:
        snapshot = (self.root / record["snapshot"]).resolve()
        if not snapshot.is_relative_to((self.state / "releases").resolve()):
            raise ReleaseError("Snapshot path is outside deployment state")
        env = dict(os.environ)
        env.update({f"{role.upper()}_IMAGE": record["images"][role] for role in ROLES})
        env["NGINX_CONFIG"] = str(snapshot / "deploy/nginx/default.conf")
        return self.run(
            [
                "docker",
                "compose",
                "--project-name",
                "vpn-panel",
                "--project-directory",
                str(self.root),
                "--env-file",
                str(self.root / ".env.prod"),
                "-f",
                str(snapshot / FILES[0]),
                "-f",
                str(snapshot / "images.override.json"),
                "--profile",
                "bot",
                *args,
            ],
            env=env,
            input=input,
            timeout=timeout,
        )

    def initial_compose(self, args: list[str]) -> str:
        return self.run(
            [
                "docker",
                "compose",
                "--project-name",
                "vpn-panel",
                "--project-directory",
                str(self.root),
                "--env-file",
                str(self.root / ".env.prod"),
                "-f",
                str(self.root / FILES[0]),
                "--profile",
                "bot",
                *args,
            ]
        )

    def snapshot(self, source: Path, name: str, images: dict[str, str]) -> str:
        destination = self.state / "releases" / name
        if destination.exists():
            raise ReleaseError("Release snapshot already exists; inspect deployment state")
        destination.mkdir(parents=True, mode=0o700)
        for filename in FILES:
            target = destination / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / filename, target)
        override: dict[str, Any] = {"services": {}}
        for service, role in (("api", "app"), ("worker", "app"), ("migrate", "app"), ("nginx", "web"), ("bot", "bot")):
            override["services"][service] = {"image": images[role]}
        override["services"]["nginx"]["volumes"] = [
            {
                "type": "bind",
                "source": str(destination / "deploy/nginx/default.conf"),
                "target": "/etc/nginx/conf.d/default.conf",
                "read_only": True,
            }
        ]
        (destination / "images.override.json").write_text(json.dumps(override), encoding="utf-8")
        return str(destination.relative_to(self.root))

    def revisions(self, record: dict[str, Any]) -> list[str]:
        result = self.sql(record, "SELECT version_num FROM alembic_version ORDER BY version_num;")
        revisions = result.splitlines()
        if not valid_revisions(revisions):
            raise ReleaseError("Cannot establish database schema revision")
        return sorted(revisions)

    def sql(self, record: dict[str, Any], query: str) -> str:
        return self.compose(
            record,
            [
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                'exec psql -X -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
            ],
            input=query.encode(),
        )

    def inspect(self, identifier: str, template: str) -> str:
        return self.run(["docker", "inspect", "--format", template, identifier])

    def capture_baseline(self) -> dict[str, Any]:
        images: dict[str, str] = {}
        containers = {}
        for service, role in (("api", "app"), ("worker", "app"), ("nginx", "web"), ("bot", "bot")):
            identifier = self.initial_compose(["ps", "-q", service])
            containers[service] = identifier
            if not identifier and service != "bot":
                raise ReleaseError("Existing api, worker and nginx must be running")
            if identifier:
                image = self.inspect(identifier, "{{.Image}}")
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                    raise ReleaseError("Cannot capture immutable baseline image")
                if role in images and images[role] != image:
                    raise ReleaseError("API and worker must use the same image")
                images[role] = image
        # An unused profile still requires a valid image reference for Compose parsing.
        images.setdefault("bot", images["app"])
        name = "legacy-" + uuid.uuid4().hex
        record: dict[str, Any] = {
            "commit": name,
            "images": images,
            "bot_enabled": bool(containers["bot"]),
            "snapshot": self.snapshot(self.root, name, images),
        }
        record["schema_heads"] = self.revisions(record)
        self.health(record)
        self.write_state("current.json", record)
        return record

    def preflight(self) -> None:
        if sys.version_info < (3, 10):
            raise ReleaseError("Release deployment requires Python 3.10 or newer")
        for name in ("panel", ".env.prod", "docker-compose.prod.yml", "certs/fullchain.pem"):
            if not (self.root / name).is_file():
                raise ReleaseError("Existing production installation is required")
        platform = self.run(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"])
        if platform not in ("linux/x86_64", "linux/amd64"):
            raise ReleaseError("Release requires a linux/amd64 Docker host")
        if shutil.disk_usage(self.root).free < 512 * 1024 * 1024:
            raise ReleaseError("At least 512 MiB free disk space is required")

    def guard_attempt(self) -> None:
        path = self.state / "attempt.json"
        if path.exists() and read_json(path).get("phase") not in ("successful", "rolled_back"):
            raise ReleaseError("An interrupted deployment requires recovery before another update")

    def pull(self, record: dict[str, Any]) -> None:
        for role in ROLES:
            if role != "bot" or record["bot_enabled"]:
                self.run(["docker", "pull", record["images"][role]], timeout=900)

    def quiesce(self, record: dict[str, Any]) -> None:
        services = ["nginx", "api"] + (["bot"] if record["bot_enabled"] else [])
        self.compose(record, ["stop", "--timeout", "30", *services])
        identifier = self.compose(record, ["ps", "-q", "worker"])
        if identifier:
            self.run(["docker", "kill", "--signal", "SIGTERM", identifier])
            deadline = time.monotonic() + 90
            while self.inspect(identifier, "{{.State.Running}}") == "true":
                if time.monotonic() >= deadline:
                    raise ReleaseError("Worker drain timed out; no migration was started")
                time.sleep(1)
            if self.inspect(identifier, "{{.State.ExitCode}}") != "0":
                raise ReleaseError("Worker did not exit cleanly; no migration was started")
        count = self.sql(record, "SELECT count(*) FROM jobs WHERE status::text IN ('RUNNING', 'running');")
        if count != "0":
            raise ReleaseError("Running jobs remain; migration is blocked")

    def backup(self, record: dict[str, Any]) -> str:
        directory = self.state / "backups" / uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        temporary = directory / ".database.part"
        destination = directory / "database.dump"
        # Keep the dump off stdout and out of memory; credentials stay in Postgres.
        snapshot = self.root / record["snapshot"]
        command = [
            "docker",
            "compose",
            "--project-name",
            "vpn-panel",
            "--project-directory",
            str(self.root),
            "--env-file",
            str(self.root / ".env.prod"),
            "-f",
            str(snapshot / FILES[0]),
            "-f",
            str(snapshot / "images.override.json"),
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc',
        ]
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                result = subprocess.run(command, cwd=self.root, stdout=handle, stderr=subprocess.PIPE, timeout=600)
            if result.returncode or temporary.stat().st_size < 5:
                raise ReleaseError("Database backup failed")
            with temporary.open("rb") as handle:
                if handle.read(5) != b"PGDMP":
                    raise ReleaseError("Database backup is not a PostgreSQL archive")
            # pg_restore reads and validates its archive catalog before publication.
            with temporary.open("rb") as handle:
                verification = subprocess.run(
                    [*command[: command.index("exec")], "exec", "-T", "postgres", "pg_restore", "--list"],
                    cwd=self.root,
                    stdin=handle,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=120,
                )
            if verification.returncode:
                raise ReleaseError("Backup archive validation failed")
            os.replace(temporary, destination)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseError("Database backup failed") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return str(destination)

    def migrate(self, record: dict[str, Any]) -> None:
        name = self.migration_container or "tollwire-migrate-" + uuid.uuid4().hex
        self.migration_container = name
        # A Docker CLI timeout does not stop a container in the daemon. A named,
        # non-auto-removed container lets us inspect and cancel the actual task.
        try:
            self.compose(record, ["run", "--name", name, "--no-deps", "-T", "migrate"], timeout=600)
            if self.inspect(name, "{{.State.Running}}") != "false":
                raise ReleaseError("Migration container has not finished")
            if self.inspect(name, "{{.State.ExitCode}}") != "0":
                raise ReleaseError("Migration container failed")
            self.run(["docker", "rm", name])
            self.migration_container = None
        except (ReleaseError, OSError, KeyboardInterrupt):
            self.migration_uncertain = True
            try:
                self.run(["docker", "rm", "-f", name])
            except (ReleaseError, OSError, KeyboardInterrupt):
                pass
            raise
        if self.revisions(record) != sorted(record["schema_heads"]):
            raise ReleaseError("Migrated database does not match release schema heads")

    def activate(self, record: dict[str, Any]) -> None:
        self.compose(record, ["up", "-d", "--no-build", "--no-deps", "api", "worker"])
        # Wait for API readiness before exposing nginx or starting the local bot.
        self.wait_service(record, "api", healthy=True)
        self.compose(
            record, ["up", "-d", "--no-build", "--no-deps", "nginx", *(["bot"] if record["bot_enabled"] else [])]
        )

    def wait_service(self, record: dict[str, Any], service: str, healthy: bool = False) -> None:
        deadline = time.monotonic() + (120 if service == "bot" else 90)
        while time.monotonic() < deadline:
            identifier = self.compose(record, ["ps", "-q", service])
            if identifier:
                try:
                    state = json.loads(self.inspect(identifier, "{{json .State}}"))
                except ValueError as exc:
                    raise ReleaseError("Cannot read container readiness state") from exc
                restarts = self.inspect(identifier, "{{.RestartCount}}")
                if state.get("Running") and restarts.isdigit():
                    if not healthy or state.get("Health", {}).get("Status") == "healthy":
                        role = "web" if service == "nginx" else "bot" if service == "bot" else "app"
                        wanted = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", record["images"][role]])
                        if self.inspect(identifier, "{{.Image}}") != wanted:
                            raise ReleaseError("Running image differs from selected release")
                        return
            time.sleep(2)
        raise ReleaseError("Service readiness check failed")

    def health(self, record: dict[str, Any]) -> None:
        if bool(self.compose(record, ["ps", "-q", "bot"])) != record["bot_enabled"]:
            raise ReleaseError("Local bot state differs from recorded release; reconcile it before deployment")
        services = ("api", "worker", "nginx", *(["bot"] if record["bot_enabled"] else []))
        counters = {}
        for service in services:
            self.wait_service(record, service, healthy=service in ("api", "bot"))
            identifier = self.compose(record, ["ps", "-q", service])
            counters[service] = self.inspect(identifier, "{{.RestartCount}}")
        # Internal proxy HTTPS; the operator verifies external certificate/hostname separately.
        command = ["exec", "-T", "nginx", "wget", "-T", "10", "-q", "--no-check-certificate", "-O"]
        self.compose(record, [*command, "/dev/null", "https://127.0.0.1/health"])
        index = self.compose(record, [*command, "-", "https://127.0.0.1/"])
        assets = sorted(set(re.findall(r"/assets/[A-Za-z0-9_.-]+\.(?:js|css)", index)))
        if not assets:
            raise ReleaseError("Frontend index has no build assets")
        for asset in assets:
            self.compose(record, [*command, "/dev/null", "https://127.0.0.1" + asset])
        time.sleep(2)
        for service in services:
            self.wait_service(record, service, healthy=service in ("api", "bot"))
            identifier = self.compose(record, ["ps", "-q", service])
            if self.inspect(identifier, "{{.RestartCount}}") != counters[service]:
                raise ReleaseError("Service restarted during deployment verification")

    def stop_all(self, record: dict[str, Any]) -> None:
        self.compose(
            record,
            ["stop", "--timeout", "90", "nginx", "api", "worker", *(["bot"] if record["bot_enabled"] else [])],
            timeout=120,
        )

    def validate_config(self, record: dict[str, Any]) -> None:
        """Reject an unusable target configuration before any running service stops."""
        self.compose(record, ["config", "--quiet"])

    def transaction(self, source: dict[str, Any], target: dict[str, Any], migration: bool) -> None:
        attempt: dict[str, Any] = {"phase": "prepared", "source": source, "target": target}
        self.migration_uncertain = False
        if migration:
            self.migration_container = "tollwire-migrate-" + uuid.uuid4().hex
            attempt["migration_container"] = self.migration_container
        self.write_state("attempt.json", attempt)
        try:
            attempt["phase"] = "draining"
            self.write_state("attempt.json", attempt)
            self.quiesce(source)
            attempt["phase"] = "backup"
            self.write_state("attempt.json", attempt)
            attempt["backup"] = self.backup(source)
            attempt["phase"] = "migrating" if migration else "activating"
            self.write_state("attempt.json", attempt)
            if migration:
                self.migrate(target)
            attempt["phase"] = "activating"
            self.write_state("attempt.json", attempt)
            self.activate(target)
            self.health(target)
            self.write_state("previous.json", source)
            self.write_state("current.json", target)
            attempt["phase"] = "successful"
            self.write_state("attempt.json", attempt)
        except (ReleaseError, OSError, KeyboardInterrupt) as exc:
            attempt["phase"] = "maintenance"
            attempt["error"] = "Deployment failed; manual recovery may be required"
            try:
                self.write_state("attempt.json", attempt)
            except OSError:
                pass
            try:
                # Both releases use the fixed vpn-panel project/service names. The
                # previous Compose file is known to parse; the target may not be.
                self.stop_all(source)
                if self.migration_uncertain:
                    raise ReleaseError("Migration completion is uncertain; automatic restart is blocked")
                if self.revisions(source) != sorted(source["schema_heads"]):
                    raise ReleaseError(
                        "Database schema changed: services remain stopped; no automatic database restore"
                    )
                self.activate(source)
                self.health(source)
                self.write_state("current.json", source)
                attempt["phase"] = "rolled_back"
                self.write_state("attempt.json", attempt)
            except (ReleaseError, OSError, KeyboardInterrupt) as recovery:
                try:
                    self.stop_all(source)
                except ReleaseError:
                    pass
                raise ReleaseError(
                    "Recovery stopped in maintenance; inspect .deploy/attempt.json and backup"
                ) from recovery
            raise ReleaseError("Deployment failed; previous release restored without changing database data") from exc

    def deploy(self, bundle: Path) -> None:
        manifest = validate_manifest(bundle)
        with self.lock():
            self.preflight()
            self.guard_attempt()
            self.install_management_guard()
            current_path = self.state / "current.json"
            source = read_json(current_path) if current_path.exists() else self.capture_baseline()
            if source["commit"] == manifest["commit"]:
                if source["images"] != manifest["images"] or source.get("files") != manifest["files"]:
                    raise ReleaseError("Installed commit has different image digests")
                self.health(source)
                print("Release already installed and healthy")
                return
            if self.revisions(source) != sorted(source["schema_heads"]):
                raise ReleaseError("Database schema differs from recorded current release")
            self.health(source)
            target = {
                **manifest,
                "bot_enabled": source["bot_enabled"],
                "snapshot": "",
            }
            self.pull(target)
            target["snapshot"] = self.snapshot(
                bundle, manifest["commit"] + "-" + uuid.uuid4().hex[:8], target["images"]
            )
            self.validate_config(target)
            self.transaction(source, target, migration=True)
            print("Release deployed and healthy")

    def rollback(self) -> None:
        with self.lock():
            self.preflight()
            self.guard_attempt()
            self.install_management_guard()
            source = read_json(self.state / "current.json")
            target = read_json(self.state / "previous.json")
            if self.revisions(source) != sorted(target["schema_heads"]):
                raise ReleaseError("Rollback blocked: previous release requires a different database schema")
            for role in ROLES:
                if role != "bot" or target["bot_enabled"]:
                    self.run(["docker", "image", "inspect", "--format", "{{.Id}}", target["images"][role]])
            self.validate_config(target)
            self.transaction(source, target, migration=False)
            print("Previous release restored; database data preserved")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("deploy", "rollback"))
    parser.add_argument("bundle", type=Path, nargs="?")
    options = parser.parse_args()
    if (options.operation == "deploy") != (options.bundle is not None):
        parser.error("deploy requires RELEASE_DIR; rollback takes no arguments")
    root = Path(os.environ.get("TOLLWIRE_ROOT", Path(__file__).resolve().parents[1]))
    manager = Deployment(root)

    def interrupted(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if options.operation == "deploy":
            manager.deploy(options.bundle)
        else:
            manager.rollback()
    except (ReleaseError, KeyboardInterrupt) as exc:
        print(str(exc) or "Deployment interrupted; inspect .deploy/attempt.json", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
