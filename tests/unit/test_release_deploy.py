"""Release manifest and deployment failure boundaries, without production Docker or secrets."""

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.unit.install_support import BASH, docker_calls, installed, requires_bash, run_panel

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40
DIGEST = "b" * 64
FILES = ("docker-compose.prod.yml", "deploy/nginx/default.conf")


@pytest.fixture
def release_module() -> ModuleType:
    path = ROOT / "scripts" / "release.py"
    assert path.is_file(), "release deployment engine is not implemented"
    spec = importlib.util.spec_from_file_location("tollwire_release_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    directory = tmp_path / "bundle"
    directory.mkdir()
    checksums = {}
    for name in FILES:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"name: vpn-panel\n" if name.endswith(".yml") else b"server {}\n")
        checksums[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "format_version": 1,
        "commit": COMMIT,
        "platform": "linux/amd64",
        "images": {role: f"ghcr.io/example/tollwire/{role}@sha256:{DIGEST}" for role in ("app", "web", "bot")},
        "schema_heads": ["a9d1e6c42b70"],
        "files": checksums,
    }
    write_manifest(directory, manifest)
    return directory


def read_manifest(bundle: Path) -> dict[str, Any]:
    return json.loads((bundle / "release.json").read_text(encoding="utf-8"))


def write_manifest(bundle: Path, manifest: Any) -> None:
    (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_manifest_accepts_matching_files_and_immutable_images(release_module: ModuleType, bundle: Path) -> None:
    manifest = release_module.validate_manifest(bundle)

    assert manifest["commit"] == COMMIT
    assert manifest["schema_heads"] == ["a9d1e6c42b70"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("format_version", 2),
        ("format_version", True),
        ("commit", "main"),
        ("commit", "a" * 39),
        ("commit", "$(touch compromised)"),
        ("platform", "linux/arm64"),
        ("schema_heads", []),
        ("schema_heads", "a9d1e6c42b70"),
        ("schema_heads", ["a9d1e6c42b70; rm -rf /"]),
        ("schema_heads", ["a9d1e6c42b70", "a9d1e6c42b70"]),
        ("images", []),
        ("files", []),
    ],
)
def test_manifest_rejects_invalid_scalar_or_collection(
    release_module: ModuleType, bundle: Path, key: str, value: Any
) -> None:
    manifest = read_manifest(bundle)
    manifest[key] = value
    write_manifest(bundle, manifest)

    with pytest.raises(release_module.ReleaseError):
        release_module.validate_manifest(bundle)


@pytest.mark.parametrize("key", ["commit", "platform", "images", "schema_heads", "files", "format_version"])
def test_manifest_requires_every_contract_field(release_module: ModuleType, bundle: Path, key: str) -> None:
    manifest = read_manifest(bundle)
    del manifest[key]
    write_manifest(bundle, manifest)

    with pytest.raises(release_module.ReleaseError):
        release_module.validate_manifest(bundle)


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/example/tollwire/app:latest",
        f"docker.io/example/tollwire/app@sha256:{DIGEST}",
        f"ghcr.io/other/tollwire/app@sha256:{DIGEST}",
        f"ghcr.io/example/tollwire/web@sha256:{DIGEST}",
        f"ghcr.io/example/tollwire/app@sha256:{DIGEST[:-1]}",
        f"ghcr.io/example/tollwire/app@sha256:{DIGEST}\n--privileged",
    ],
)
def test_manifest_rejects_mutable_foreign_or_injected_image(
    release_module: ModuleType, bundle: Path, image: str
) -> None:
    manifest = read_manifest(bundle)
    manifest["images"]["app"] = image
    write_manifest(bundle, manifest)

    with pytest.raises(release_module.ReleaseError):
        release_module.validate_manifest(bundle)


@pytest.mark.parametrize("value", [None, [], "text", 17])
def test_manifest_requires_an_object(release_module: ModuleType, bundle: Path, value: Any) -> None:
    write_manifest(bundle, value)

    with pytest.raises(release_module.ReleaseError):
        release_module.validate_manifest(bundle)


def test_manifest_rejects_unlisted_path_traversal(release_module: ModuleType, bundle: Path) -> None:
    outside = bundle.parent / "outside"
    outside.write_bytes(b"untrusted")
    manifest = read_manifest(bundle)
    manifest["files"]["../outside"] = hashlib.sha256(b"untrusted").hexdigest()
    write_manifest(bundle, manifest)

    with pytest.raises(release_module.ReleaseError):
        release_module.validate_manifest(bundle)


@pytest.mark.parametrize("name", FILES)
def test_manifest_rejects_modified_runtime_configuration(release_module: ModuleType, bundle: Path, name: str) -> None:
    (bundle / name).write_bytes(b"modified after packaging")

    with pytest.raises(release_module.ReleaseError):
        release_module.validate_manifest(bundle)


@pytest.mark.parametrize("name", FILES)
def test_manifest_rejects_missing_runtime_configuration(release_module: ModuleType, bundle: Path, name: str) -> None:
    (bundle / name).unlink()

    with pytest.raises(release_module.ReleaseError):
        release_module.validate_manifest(bundle)


def test_manifest_rejects_symlinked_runtime_configuration(release_module: ModuleType, bundle: Path) -> None:
    path = bundle / FILES[0]
    source = bundle.parent / "external-compose.yml"
    source.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation is unavailable for this Windows user")

    with pytest.raises(release_module.ReleaseError):
        release_module.validate_manifest(bundle)


def test_manifest_rejects_duplicate_json_keys(release_module: ModuleType, bundle: Path) -> None:
    text = (bundle / "release.json").read_text(encoding="utf-8")
    text = text.replace('"format_version": 1', '"format_version": 2, "format_version": 1')
    (bundle / "release.json").write_text(text, encoding="utf-8")

    with pytest.raises(release_module.ReleaseError):
        release_module.validate_manifest(bundle)


@requires_bash
@pytest.mark.slow
def test_backup_print_path_is_machine_readable_and_does_not_mix_status_messages(app: Path) -> None:
    installed(app)

    result = run_panel(app, "backup", "--print-path")

    assert result.returncode == 0, result.stderr
    paths = result.stdout.splitlines()
    assert len(paths) == 1
    dump = app / paths[0]
    assert dump.is_file() and dump.read_bytes().startswith(b"PGDMP")
    assert "ENCRYPTION_KEY" not in result.stdout
    assert "ENCRYPTION_KEY" in result.stderr


@requires_bash
@pytest.mark.slow
def test_failed_backup_print_path_never_reports_a_successful_path(app: Path) -> None:
    installed(app)

    result = run_panel(app, "backup", "--print-path", FAKE_DUMP_FAIL="1")

    assert result.returncode != 0
    assert result.stdout == ""
    assert not list((app / "backups").glob("*.dump"))


@requires_bash
@pytest.mark.slow
def test_legacy_update_refuses_release_managed_installation_before_docker_or_git(app: Path) -> None:
    installed(app)
    (app / ".deploy").mkdir()
    (app / ".deploy/current.json").write_text("{}\n", encoding="utf-8")
    fake_git = app.parent / "bin/git"
    fake_git.write_text('#!/usr/bin/env bash\necho git-called >> "$FAKE_DOCKER_LOG"\nexit 99\n', encoding="utf-8")
    fake_git.chmod(0o755)

    result = run_panel(app, "update")

    assert result.returncode != 0
    assert "scripts/deploy.sh" in result.stderr
    assert docker_calls(app) == []


@requires_bash
@pytest.mark.slow
def test_legacy_restore_refuses_release_managed_installation_before_database_commands(app: Path) -> None:
    installed(app)
    (app / ".deploy").mkdir()
    (app / ".deploy/current.json").write_text("{}\n", encoding="utf-8")
    dump = app / "fixture.dump"
    dump.write_bytes(b"PGDMP-fixture")

    result = run_panel(app, "restore", "fixture.dump", "--yes")

    assert result.returncode != 0
    assert result.stderr
    assert docker_calls(app) == []
    assert dump.read_bytes() == b"PGDMP-fixture"


@pytest.fixture
def legacy_panel(release_module: ModuleType, tmp_path: Path) -> tuple[Any, bytes]:
    root = tmp_path / "legacy installation"
    root.mkdir()
    (root / ".deploy").mkdir()
    original = (
        b"#!/usr/bin/env bash\n"
        b'root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)\n'
        b'printf "original:%s\\n" "${1:-}"\n'
        b'printf "executed" > "$root/executed"\n'
    )
    panel = root / "panel"
    panel.write_bytes(original)
    panel.chmod(0o750)
    return release_module.Deployment(root), original


def test_bootstrap_guard_preserves_original_panel_and_is_idempotent(legacy_panel: tuple[Any, bytes]) -> None:
    manager, original = legacy_panel
    panel = manager.root / "panel"
    old_mode = panel.stat().st_mode

    manager.install_management_guard()
    guarded = panel.read_bytes()
    manager.install_management_guard()

    assert panel.read_bytes() == guarded
    assert guarded.endswith(original.split(b"\n", 1)[1])
    backup = manager.state / "legacy-panel"
    assert backup.read_bytes() == original
    if os.name == "posix":
        assert panel.stat().st_mode == old_mode
        assert backup.stat().st_mode & 0o777 == 0o600


@requires_bash
@pytest.mark.slow
@pytest.mark.parametrize("operation", ["update", "restore", "bot"])
def test_bootstrap_guard_blocks_mutations_before_original_legacy_body(
    legacy_panel: tuple[Any, bytes], operation: str
) -> None:
    manager, _ = legacy_panel
    manager.install_management_guard()
    (manager.state / "current.json").write_text("{}\n", encoding="utf-8")

    result = subprocess.run(
        [str(BASH), str(manager.root / "panel"), operation],
        cwd=manager.root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert not (manager.root / "executed").exists()


@requires_bash
@pytest.mark.slow
def test_bootstrap_guard_keeps_backup_running_from_original_panel_location(legacy_panel: tuple[Any, bytes]) -> None:
    manager, _ = legacy_panel
    manager.install_management_guard()
    (manager.state / "current.json").write_text("{}\n", encoding="utf-8")

    result = subprocess.run(
        [str(BASH), str(manager.root / "panel"), "backup"],
        cwd=manager.root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "original:backup\n"
    assert (manager.root / "executed").read_text(encoding="utf-8") == "executed"


@pytest.fixture
def manager(release_module: ModuleType, bundle: Path, tmp_path: Path) -> Any:
    """Fake external boundaries; transaction/state writes and snapshots remain real."""

    class FakeDeployment(release_module.Deployment):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.events: list[str] = []
            self.fail: str | None = None
            self.live_heads = ["a9d1e6c42b70"]
            self.bot_enabled = False

        @contextlib.contextmanager
        def lock(self) -> Any:
            self.state.mkdir(mode=0o700, exist_ok=True)
            yield

        def preflight(self) -> None:
            self.events.append("preflight")

        def install_management_guard(self) -> None:
            pass

        def run(self, args: list[str], **kwargs: Any) -> str:
            if args[:2] == ["docker", "pull"]:
                image = args[2]
                role = image.split("@", 1)[0].rsplit("/", 1)[-1]
                self.events.append(f"pull:{role}")
                if self.fail == "pull":
                    raise release_module.ReleaseError("fixture registry unavailable")
                return ""
            if args[:3] == ["docker", "image", "inspect"]:
                self.events.append("inspect-previous-image")
                return "sha256:" + "c" * 64
            raise AssertionError(f"Unexpected external command: {args!r}")

        def capture_baseline(self) -> dict[str, Any]:
            record = {
                "commit": "legacy-fixture",
                "images": {role: "sha256:" + "c" * 64 for role in ("app", "web", "bot")},
                "schema_heads": self.live_heads.copy(),
                "bot_enabled": self.bot_enabled,
                "snapshot": self.snapshot(self.root, "legacy-fixture", {}),
            }
            self.write_state("current.json", record)
            return record

        def snapshot(self, source: Path, name: str, images: dict[str, str]) -> str:
            # Snapshot itself is production code; pass real immutable refs for its override.
            if not images:
                images = {role: "sha256:" + "c" * 64 for role in ("app", "web", "bot")}
            return super().snapshot(source, name, images)

        def revisions(self, record: dict[str, Any]) -> list[str]:
            return self.live_heads.copy()

        def quiesce(self, record: dict[str, Any]) -> None:
            self.events.append("quiesce")
            if self.fail == "drain":
                raise release_module.ReleaseError("fixture worker drain failed")

        def backup(self, record: dict[str, Any]) -> str:
            self.events.append("backup")
            if self.fail == "backup":
                raise release_module.ReleaseError("fixture backup failed")
            path = self.state / "fixture.dump"
            path.write_bytes(b"PGDMP-fixture")
            return str(path)

        def validate_config(self, record: dict[str, Any]) -> None:
            self.events.append("config")
            if self.fail == "config":
                raise release_module.ReleaseError("fixture target Compose invalid")

        def migrate(self, record: dict[str, Any]) -> None:
            self.events.append("migrate")
            if self.fail == "migration-uncertain":
                self.migration_uncertain = True
                raise release_module.ReleaseError("fixture migration CLI timed out")
            if self.fail == "migration-before":
                raise release_module.ReleaseError("fixture migration did not start")
            self.live_heads = list(record["schema_heads"])
            if self.fail == "migration-after":
                raise release_module.ReleaseError("fixture migration partially applied")

        def activate(self, record: dict[str, Any]) -> None:
            self.events.append(f"activate:{record['commit']}")
            if self.fail == "activate" and record["commit"] == COMMIT:
                raise release_module.ReleaseError("fixture target start failed")

        def health(self, record: dict[str, Any]) -> None:
            self.events.append(f"health:{record['commit']}")
            if self.fail == "health" and record["commit"] == COMMIT:
                raise release_module.ReleaseError("fixture target unhealthy")

        def stop_all(self, record: dict[str, Any]) -> None:
            self.events.append(f"stop:{record['commit']}")

    root = tmp_path / "installation"
    root.mkdir()
    for name in FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle / name, target)
    return FakeDeployment(root)


def state(manager: Any, name: str) -> dict[str, Any]:
    return json.loads((manager.state / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("bot_enabled", [False, True])
def test_deploy_pulls_before_downtime_and_promotes_only_healthy_release(
    manager: Any, bundle: Path, bot_enabled: bool
) -> None:
    manager.bot_enabled = bot_enabled

    manager.deploy(bundle)

    events = manager.events
    assert events.index("pull:app") < events.index("quiesce")
    assert events.index("pull:web") < events.index("quiesce")
    assert events.index("config") < events.index("quiesce")
    assert ("pull:bot" in events) == bot_enabled
    assert events.index("quiesce") < events.index("backup") < events.index("migrate")
    assert events.index("migrate") < events.index(f"activate:{COMMIT}") < events.index(f"health:{COMMIT}")
    current = state(manager, "current.json")
    previous = state(manager, "previous.json")
    assert current["commit"] == COMMIT and current["bot_enabled"] == bot_enabled
    assert previous["commit"] == "legacy-fixture"
    assert state(manager, "attempt.json")["phase"] == "successful"
    assert (manager.root / current["snapshot"] / FILES[0]).is_file()
    assert (manager.root / previous["snapshot"] / FILES[0]).is_file()


def test_registry_failure_never_stops_working_installation(
    manager: Any, bundle: Path, release_module: ModuleType
) -> None:
    manager.fail = "pull"

    with pytest.raises(release_module.ReleaseError):
        manager.deploy(bundle)

    assert "quiesce" not in manager.events
    assert "backup" not in manager.events and "migrate" not in manager.events
    assert not any(event.startswith("stop:") for event in manager.events)
    assert state(manager, "current.json")["commit"] == "legacy-fixture"


@pytest.mark.parametrize("failure", ["drain", "backup", "migration-before", "activate", "health"])
def test_failure_with_unchanged_schema_restores_previous_release_without_database_restore(
    manager: Any, bundle: Path, release_module: ModuleType, failure: str
) -> None:
    manager.fail = failure

    with pytest.raises(release_module.ReleaseError):
        manager.deploy(bundle)

    assert state(manager, "current.json")["commit"] == "legacy-fixture"
    assert state(manager, "attempt.json")["phase"] == "rolled_back"
    assert manager.events[-2:] == ["activate:legacy-fixture", "health:legacy-fixture"]
    assert not (manager.state / "previous.json").exists()
    assert manager.live_heads == ["a9d1e6c42b70"]
    if failure == "drain":
        assert "backup" not in manager.events
    if failure in ("drain", "backup"):
        assert "migrate" not in manager.events


@pytest.mark.parametrize("failure", ["migration-after", "health"])
def test_failure_after_schema_change_keeps_writers_stopped_and_preserves_backup(
    manager: Any, bundle: Path, release_module: ModuleType, failure: str
) -> None:
    manifest = read_manifest(bundle)
    manifest["schema_heads"] = ["new_revision_123"]
    write_manifest(bundle, manifest)
    manager.fail = failure

    with pytest.raises(release_module.ReleaseError):
        manager.deploy(bundle)

    attempt = state(manager, "attempt.json")
    assert attempt["phase"] == "maintenance"
    assert Path(attempt["backup"]).read_bytes() == b"PGDMP-fixture"
    assert manager.live_heads == ["new_revision_123"]
    assert "activate:legacy-fixture" not in manager.events
    assert manager.events[-1] == "stop:legacy-fixture"
    assert state(manager, "current.json")["commit"] == "legacy-fixture"


def test_repeated_deploy_checks_health_without_restarting_services(manager: Any, bundle: Path) -> None:
    manager.deploy(bundle)
    manager.events.clear()

    manager.deploy(bundle)

    assert manager.events == ["preflight", f"health:{COMMIT}"]


def test_noop_deploy_rejects_changed_images_for_same_commit(
    manager: Any, bundle: Path, release_module: ModuleType
) -> None:
    manager.deploy(bundle)
    manager.events.clear()
    manifest = read_manifest(bundle)
    manifest["images"]["app"] = f"ghcr.io/example/tollwire/app@sha256:{'d' * 64}"
    write_manifest(bundle, manifest)

    with pytest.raises(release_module.ReleaseError):
        manager.deploy(bundle)

    assert "quiesce" not in manager.events
    assert state(manager, "current.json")["images"]["app"].endswith(DIGEST)


def test_noop_deploy_rejects_changed_runtime_hashes_for_same_commit(
    manager: Any, bundle: Path, release_module: ModuleType
) -> None:
    manager.deploy(bundle)
    original = state(manager, "current.json")
    manager.events.clear()
    path = bundle / "deploy/nginx/default.conf"
    path.write_bytes(b"# replaced configuration\nserver {}\n")
    manifest = read_manifest(bundle)
    manifest["files"]["deploy/nginx/default.conf"] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_manifest(bundle, manifest)

    with pytest.raises(release_module.ReleaseError):
        manager.deploy(bundle)

    assert "quiesce" not in manager.events
    assert not any(event.startswith("pull:") for event in manager.events)
    assert state(manager, "current.json")["files"] == original["files"]


def test_interrupted_attempt_blocks_another_deploy_before_pulling_or_stopping(
    manager: Any, bundle: Path, release_module: ModuleType
) -> None:
    manager.write_state("attempt.json", {"phase": "migrating"})

    with pytest.raises(release_module.ReleaseError):
        manager.deploy(bundle)

    assert manager.events == ["preflight"]
    assert state(manager, "attempt.json")["phase"] == "migrating"


def test_rollback_with_same_schema_preserves_data_and_skips_migration(manager: Any, bundle: Path) -> None:
    manager.deploy(bundle)
    manager.events.clear()

    manager.rollback()

    assert "migrate" not in manager.events
    assert "backup" in manager.events
    assert state(manager, "current.json")["commit"] == "legacy-fixture"
    assert state(manager, "previous.json")["commit"] == COMMIT


def test_rollback_across_schema_change_fails_before_downtime(
    manager: Any, bundle: Path, release_module: ModuleType
) -> None:
    manifest = read_manifest(bundle)
    manifest["schema_heads"] = ["new_revision_123"]
    write_manifest(bundle, manifest)
    manager.deploy(bundle)
    manager.events.clear()

    with pytest.raises(release_module.ReleaseError):
        manager.rollback()

    assert manager.events == ["preflight"]
    assert state(manager, "current.json")["commit"] == COMMIT
    assert manager.live_heads == ["new_revision_123"]


def test_database_drift_blocks_update_before_pulling_or_stopping(
    manager: Any, bundle: Path, release_module: ModuleType
) -> None:
    manager.capture_baseline()
    manager.live_heads = ["manually_changed_schema"]

    with pytest.raises(release_module.ReleaseError):
        manager.deploy(bundle)

    assert "quiesce" not in manager.events
    assert not any(event.startswith("pull:") for event in manager.events)


def test_invalid_target_compose_aborts_before_downtime_and_preserves_current_release(
    manager: Any, bundle: Path, release_module: ModuleType
) -> None:
    manager.fail = "config"

    with pytest.raises(release_module.ReleaseError):
        manager.deploy(bundle)

    assert "config" in manager.events
    assert "quiesce" not in manager.events
    assert "backup" not in manager.events and "migrate" not in manager.events
    assert not any(event.startswith("stop:") or event.startswith("activate:") for event in manager.events)
    assert state(manager, "current.json")["commit"] == "legacy-fixture"
    assert not (manager.state / "attempt.json").exists()


def test_uncertain_migration_keeps_maintenance_even_if_schema_still_looks_unchanged(
    manager: Any, bundle: Path, release_module: ModuleType
) -> None:
    manager.fail = "migration-uncertain"

    with pytest.raises(release_module.ReleaseError):
        manager.deploy(bundle)

    assert manager.live_heads == ["a9d1e6c42b70"]
    assert state(manager, "attempt.json")["phase"] == "maintenance"
    assert state(manager, "current.json")["commit"] == "legacy-fixture"
    assert "activate:legacy-fixture" not in manager.events
    assert manager.events[-1] == "stop:legacy-fixture"


def test_migration_cli_failure_cancels_named_container_and_records_uncertainty(
    release_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = release_module.Deployment(tmp_path)
    compose_calls: list[list[str]] = []
    cleanup_calls: list[list[str]] = []

    def failed_compose(record: dict[str, Any], arguments: list[str], **kwargs: Any) -> str:
        compose_calls.append(arguments)
        raise release_module.ReleaseError("fixture Docker CLI timed out")

    monkeypatch.setattr(manager, "compose", failed_compose)
    monkeypatch.setattr(manager, "run", lambda arguments, **kwargs: cleanup_calls.append(arguments) or "")

    with pytest.raises(release_module.ReleaseError):
        manager.migrate({"schema_heads": ["a9d1e6c42b70"]})

    (command,) = compose_calls
    name = command[command.index("--name") + 1]
    assert name.startswith("tollwire-migrate-")
    assert "--rm" not in command and "--no-deps" in command
    assert cleanup_calls == [["docker", "rm", "-f", name]]
    assert manager.migration_uncertain is True


def test_target_compose_validation_uses_quiet_config_without_expanding_secrets(
    release_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = release_module.Deployment(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(manager, "compose", lambda record, arguments, **kwargs: calls.append(arguments) or "")

    manager.validate_config({})

    assert calls == [["config", "--quiet"]]


@pytest.mark.parametrize("exit_code,running_jobs", [("137", "0"), ("0", "1")])
def test_quiesce_blocks_migration_after_unclean_worker_or_running_jobs(
    release_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: str, running_jobs: str
) -> None:
    manager = release_module.Deployment(tmp_path)
    calls: list[list[str]] = []

    def compose(record: dict[str, Any], arguments: list[str], **kwargs: Any) -> str:
        calls.append(arguments)
        return "worker-id" if arguments == ["ps", "-q", "worker"] else ""

    def inspect(identifier: str, template: str) -> str:
        return "false" if template == "{{.State.Running}}" else exit_code

    monkeypatch.setattr(manager, "compose", compose)
    monkeypatch.setattr(manager, "inspect", inspect)
    monkeypatch.setattr(manager, "sql", lambda *args: running_jobs)
    monkeypatch.setattr(manager, "run", lambda arguments, **kwargs: calls.append(arguments) or "")

    with pytest.raises(release_module.ReleaseError):
        manager.quiesce({"bot_enabled": False})

    assert ["docker", "kill", "--signal", "SIGTERM", "worker-id"] in calls
    assert not any("migrate" in arguments for arguments in calls)


def test_worker_drain_timeout_is_bounded_without_force_killing_worker(
    release_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = release_module.Deployment(tmp_path)
    calls: list[list[str]] = []
    clock = iter([0, 1000])
    monkeypatch.setattr(release_module, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))
    monkeypatch.setattr(manager, "compose", lambda record, arguments, **kwargs: "worker-id")
    monkeypatch.setattr(manager, "inspect", lambda *args: "true")
    monkeypatch.setattr(manager, "run", lambda arguments, **kwargs: calls.append(arguments) or "")

    with pytest.raises(release_module.ReleaseError, match="[Tt]imed out"):
        manager.quiesce({"bot_enabled": False})

    assert calls == [["docker", "kill", "--signal", "SIGTERM", "worker-id"]]


@pytest.mark.parametrize("bot_enabled", [False, True])
def test_activate_uses_selected_services_without_building_or_rerunning_migrations(
    release_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bot_enabled: bool
) -> None:
    manager = release_module.Deployment(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(manager, "compose", lambda record, arguments, **kwargs: calls.append(arguments) or "")
    monkeypatch.setattr(manager, "wait_service", lambda *args, **kwargs: None)

    manager.activate({"bot_enabled": bot_enabled})

    assert calls == [
        ["up", "-d", "--no-build", "--no-deps", "api", "worker"],
        ["up", "-d", "--no-build", "--no-deps", "nginx", *(["bot"] if bot_enabled else [])],
    ]


def test_command_failure_does_not_echo_child_output_or_environment(
    release_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = release_module.Deployment(tmp_path)
    synthetic_secret = b"fixture-token-do-not-print"
    monkeypatch.setattr(
        release_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, synthetic_secret, synthetic_secret),
    )

    with pytest.raises(release_module.ReleaseError) as captured:
        manager.run(["docker", "info"])

    assert synthetic_secret.decode() not in str(captured.value)


@pytest.mark.skipif(sys.platform != "linux", reason="deployment lock uses Linux flock")
def test_parallel_deployment_cannot_acquire_the_same_lock(release_module: ModuleType, tmp_path: Path) -> None:
    first = release_module.Deployment(tmp_path)
    second = release_module.Deployment(tmp_path)

    with first.lock():
        with pytest.raises(release_module.ReleaseError, match="[Aa]nother deployment"):
            with second.lock():
                pytest.fail("a second process can change deployment state")

    # The persistent lock inode remains usable after release.
    with second.lock():
        assert (second.state / "lock").is_file()
