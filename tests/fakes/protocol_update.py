"""Image/container state for update tests; unknown commands still fail in FakeServer."""

import json
import re
import shlex
from dataclasses import dataclass, field

from infrastructure.ssh.client import CommandResult
from tests.fakes.fake_ssh import FakeServer

LATEST = "amneziavpn/amneziawg-go:latest"
DIGEST = "amneziavpn/amneziawg-go@sha256:" + "b" * 64
OLD_IMAGE = "sha256:" + "a" * 64
NEW_IMAGE = "sha256:" + "c" * 64


@dataclass
class UpdateServer(FakeServer):
    image_ids: dict[str, str] = field(default_factory=dict)
    container_ids: dict[str, str] = field(default_factory=dict)
    target_image_id: str = NEW_IMAGE
    target_digest: str = DIGEST
    extra_repo_digests: list[str] = field(default_factory=list)
    pull_fails: bool = False
    fail_new_start: bool = False
    mutate_config_on_start: bool = False
    crash_after_rename: bool = False
    fail_old_start: bool = False
    crash_during_old_start: bool = False
    fail_cleanup_once: bool = False

    def container_name(self, value: str) -> str:
        return next((name for name, cid in self.container_ids.items() if cid == value), value)

    def run(self, command: str) -> CommandResult:
        cmd = " ".join(command.split())
        name = self.layout.container_name
        directory = self.layout.host_config_dir
        snapshot = directory + "-update-snapshot"
        journal = directory + "/.container-update.json"
        cid_path = directory + "/.container-update-new.cid"
        if cmd == f"test ! -e {snapshot} && test ! -L {snapshot} && test ! -e {cid_path}":
            self.commands.append(command)
            exists = snapshot in self.dirs or cid_path in self.files or any(
                path.startswith(snapshot + "/") for path in self.files
            )
            return CommandResult("", "", int(exists))
        if cmd.startswith("docker ps -a --no-trunc "):
            self.commands.append(command)
            args = shlex.split(cmd)
            target = args[args.index("--filter") + 1].removeprefix("name=^/?").removesuffix("$")
            return CommandResult(self.container_ids.get(target, "") + "\n", "", 0)
        if cmd == f"mv -f {journal}.tmp {journal}":
            self.commands.append(command)
            self.files[journal] = self.files.pop(journal + ".tmp")
            return CommandResult("", "", 0)
        if cmd == (
            f"test ! -e {snapshot} && test ! -L {directory} && "
            f"mkdir -m 700 {snapshot} && cp -a {directory}/. {snapshot}/ && "
            f"rm -f -- {snapshot}/.container-update.json {snapshot}/.container-update.json.tmp && "
            f"chmod 700 {snapshot}"
        ):
            self.commands.append(command)
            if snapshot in self.dirs:
                return CommandResult("", "snapshot exists", 1)
            self.dirs[snapshot] = 0o700
            self.files.update(
                {
                    snapshot + path[len(directory) :]: text
                    for path, text in list(self.files.items())
                    if path.startswith(directory + "/")
                }
            )
            self.files.pop(snapshot + "/.container-update.json", None)
            self.files.pop(snapshot + "/.container-update.json.tmp", None)
            return CommandResult("", "", 0)
        if cmd == (
            f"test -d {snapshot} && test ! -L {snapshot} && test ! -L {directory} && "
            f"find {directory} -mindepth 1 -maxdepth 1 "
            "! -name '.container-update.json' ! -name '.container-update-new.cid' "
            "! -name '.container-update.json.tmp' -exec rm -rf -- {} + && "
            f"cp -a {snapshot}/. {directory}/"
        ):
            self.commands.append(command)
            assert snapshot in self.dirs
            self.files = {
                path: text
                for path, text in self.files.items()
                if not path.startswith(directory + "/") or path in {journal, journal + ".tmp", cid_path}
            }
            self.files.update(
                {
                    directory + path[len(snapshot) :]: text
                    for path, text in list(self.files.items())
                    if path.startswith(snapshot + "/")
                }
            )
            return CommandResult("", "", 0)
        if cmd == (
            f"diff -qr --exclude=.container-update.json --exclude=.container-update.json.tmp "
            f"--exclude=.container-update-new.cid {snapshot} {directory}"
        ):
            self.commands.append(command)
            current = {
                path[len(directory) :]: text
                for path, text in self.files.items()
                if path.startswith(directory + "/") and path not in {journal, journal + ".tmp", cid_path}
            }
            saved = {
                path[len(snapshot) :]: text for path, text in self.files.items() if path.startswith(snapshot + "/")
            }
            return CommandResult("", "", int(current != saved))
        if cmd == f"rm -rf -- {snapshot} && rm -f -- {journal} {journal}.tmp {cid_path}":
            self.commands.append(command)
            if self.fail_cleanup_once:
                self.fail_cleanup_once = False
                return CommandResult("", "simulated cleanup failure", 1)
            self.files = {
                path: text
                for path, text in self.files.items()
                if not path.startswith(snapshot + "/") and path not in {journal, journal + ".tmp", cid_path}
            }
            self.dirs.pop(snapshot, None)
            return CommandResult("", "", 0)
        if cmd == f"docker exec {name} awg show {self.layout.interface_name} peers":
            self.commands.append(command)
            return CommandResult("\n".join(sorted(self.interface_peers)), "", 0 if self.running else 1)
        if cmd == f"docker pull {LATEST}":
            self.commands.append(command)
            return CommandResult(
                "", "registry unavailable SECRET-MARKER" if self.pull_fails else "", int(self.pull_fails)
            )
        if "inspect" in cmd and "{{.Image}}" in cmd:
            self.commands.append(command)
            container = self.container_name(shlex.split(cmd)[-1])
            return CommandResult(self.image_ids.get(container, OLD_IMAGE) + "\n", "", 0)
        if "inspect" in cmd and "{{range .Mounts}}" in cmd:
            self.commands.append(command)
            container = self.container_name(shlex.split(cmd)[-1])
            mounts = self.mounts.get(container, [directory] if container == name else [])
            return CommandResult(" ".join(mounts) + "\n", "", 0 if container in self.containers else 1)
        if cmd.startswith("docker image inspect ") and ("RepoDigests" in cmd or "{{.Id}}" in cmd):
            self.commands.append(command)
            if "RepoDigests" in cmd:
                digests = [self.target_digest, *self.extra_repo_digests]
                value = json.dumps(digests) if "json" in cmd else self.target_digest
            else:
                value = self.target_image_id
            return CommandResult(value + "\n", "", 0)
        if match := re.fullmatch(r"docker rename (\S+) (\S+)", cmd):
            self.commands.append(command)
            old, new = self.container_name(match[1]), match[2]
            assert old in self.containers and new not in self.containers
            self.containers[new] = self.containers.pop(old)
            self.image_ids[new] = self.image_ids.pop(old, OLD_IMAGE)
            self.container_ids[new] = self.container_ids.pop(old)
            for mapping in (self.mounts, self.container_commands, self.container_ports, self.container_sysctls):
                if old in mapping:
                    mapping[new] = mapping.pop(old)
            if self.crash_after_rename:
                self.crash_after_rename = False
                raise KeyboardInterrupt("simulated worker death after rename")
            return CommandResult("", "", 0)
        if match := re.fullmatch(r"docker stop (\S+)", cmd):
            self.commands.append(command)
            self.containers[self.container_name(match[1])] = "exited"
            return CommandResult("", "", 0)
        if match := re.fullmatch(r"docker rm(?: -f)? (\S+)", cmd):
            self.commands.append(command)
            container = self.container_name(match[1])
            self.containers.pop(container, None)
            self.image_ids.pop(container, None)
            self.container_ids.pop(container, None)
            for mapping in (self.mounts, self.container_commands, self.container_ports, self.container_sysctls):
                mapping.pop(container, None)
            return CommandResult("", "", 0)
        if match := re.fullmatch(r"docker start (\S+)", cmd):
            resolved = self.container_name(match[1])
            if resolved == name:
                self.commands.append(command)
                if self.crash_during_old_start and self.image_ids.get(name) == OLD_IMAGE:
                    self.crash_during_old_start = False
                    raise KeyboardInterrupt("simulated worker death during rollback")
                if self.fail_old_start and self.image_ids.get(name) == OLD_IMAGE:
                    return CommandResult("", "old start failed", 1)
                self._start()
                return CommandResult("", "", 0)
        if cmd.startswith(f"docker run -d --name {name} "):
            result = super().run(command)
            if result.exit_status == 0:
                self.image_ids[name] = self.target_image_id if self.target_digest in cmd else OLD_IMAGE
                self.container_ids[name] = ("2" if "--cidfile" in cmd else "1") * 64
                if "--cidfile" in cmd:
                    self.files[cid_path] = self.container_ids[name] + "\n"
                if self.mutate_config_on_start:
                    self.files[self.config_file] = "corrupted by new image\n"
                    self.files[f"{self.layout.host_config_dir}/new-image.tmp"] = "temporary secret"
                if self.fail_new_start:
                    self.containers[name] = "exited"
            return result
        return super().run(command)
