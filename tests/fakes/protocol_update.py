"""Image/container state for update tests; unknown commands still fail in FakeServer."""

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
    target_image_id: str = NEW_IMAGE
    target_digest: str = DIGEST
    pull_fails: bool = False
    fail_new_start: bool = False
    mutate_config_on_start: bool = False
    crash_after_rename: bool = False

    def run(self, command: str) -> CommandResult:
        cmd = " ".join(command.split())
        name = self.layout.container_name
        if cmd == f"docker pull {LATEST}":
            self.commands.append(command)
            return CommandResult(
                "", "registry unavailable SECRET-MARKER" if self.pull_fails else "", int(self.pull_fails)
            )
        if "inspect" in cmd and "{{.Image}}" in cmd:
            self.commands.append(command)
            container = shlex.split(cmd)[-1]
            return CommandResult(self.image_ids.get(container, OLD_IMAGE) + "\n", "", 0)
        if cmd.startswith("docker image inspect ") and ("RepoDigests" in cmd or "{{.Id}}" in cmd):
            self.commands.append(command)
            if "RepoDigests" in cmd:
                import json

                value = json.dumps([self.target_digest]) if "json" in cmd else self.target_digest
            else:
                value = self.target_image_id
            return CommandResult(value + "\n", "", 0)
        if match := re.fullmatch(r"docker rename (\S+) (\S+)", cmd):
            self.commands.append(command)
            old, new = match.groups()
            assert old in self.containers and new not in self.containers
            self.containers[new] = self.containers.pop(old)
            self.image_ids[new] = self.image_ids.pop(old, OLD_IMAGE)
            for mapping in (self.mounts, self.container_commands, self.container_ports, self.container_sysctls):
                if old in mapping:
                    mapping[new] = mapping.pop(old)
            if self.crash_after_rename:
                self.crash_after_rename = False
                raise KeyboardInterrupt("simulated worker death after rename")
            return CommandResult("", "", 0)
        if match := re.fullmatch(r"docker stop (\S+)", cmd):
            self.commands.append(command)
            self.containers[match[1]] = "exited"
            return CommandResult("", "", 0)
        if match := re.fullmatch(r"docker rm(?: -f)? (\S+)", cmd):
            self.commands.append(command)
            container = match[1]
            self.containers.pop(container, None)
            self.image_ids.pop(container, None)
            for mapping in (self.mounts, self.container_commands, self.container_ports, self.container_sysctls):
                mapping.pop(container, None)
            return CommandResult("", "", 0)
        if cmd.startswith(f"docker run -d --name {name} "):
            result = super().run(command)
            if result.exit_status == 0:
                self.image_ids[name] = self.target_image_id if self.target_digest in cmd else OLD_IMAGE
                if self.mutate_config_on_start:
                    self.files[self.config_file] = "corrupted by new image\n"
                    self.files[f"{self.layout.host_config_dir}/new-image.tmp"] = "temporary secret"
                if self.fail_new_start:
                    self.containers[name] = "exited"
            return result
        return super().run(command)
