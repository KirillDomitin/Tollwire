"""Recoverable replacement of a panel-owned container. Caller holds the server lock.

The old container and a private directory snapshot survive until commit. A daemon-written
cidfile identifies a new container even if the SSH response or the worker is lost.
"""

import asyncio
import json
import re
import shlex
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from core.ssh_exception import ContainerUpdateError, DockerError
from infrastructure.vpn.awg_config import interface_values
from infrastructure.vpn.config import AMNEZIAWG_IMAGE, AMNEZIAWG_UPDATE_IMAGE
from infrastructure.vpn.provider import ProtocolUpdateResult

if TYPE_CHECKING:
    from infrastructure.vpn.amnezia import AmneziaWGProvider

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_DIGEST = re.compile(r"amneziavpn/amneziawg-go@sha256:[0-9a-f]{64}")


def valid_image_id(value: str) -> bool:
    return bool(_IMAGE_ID.fullmatch(value))


class ContainerUpdater:
    def __init__(self, provider: "AmneziaWGProvider"):
        self.provider = provider
        self.ssh = provider.ssh_client
        self.layout = provider.layout
        self.name = self.layout.container_name
        self.backup = self.name + "-update-backup"
        self.directory = self.layout.host_config_dir
        self.snapshot = self.directory + "-update-snapshot"
        self.journal_path = self.directory + "/.container-update.json"
        self.cid_path = self.directory + "/.container-update-new.cid"
        self.result = ProtocolUpdateResult(False, None, None, None)

    async def checked(self, command: str, *, timeout: int | None = None) -> str:
        result = await self.ssh.execute(command, timeout=timeout)
        if result.exit_status:
            raise DockerError("Container update command failed")
        return result.stdout.strip()

    async def container_id(self, name: str) -> str | None:
        # ps returning no row distinguishes absence from inspect/daemon failure.
        output = await self.checked(
            f"docker ps -a --no-trunc --filter 'name=^/?{name}$' --format '{{{{.ID}}}}'"
        )
        if not output:
            return None
        if not _CONTAINER_ID.fullmatch(output):
            raise DockerError("Invalid container identity")
        return output

    async def image_id(self, container: str) -> str:
        output = await self.checked(f"docker inspect --type container -f '{{{{.Image}}}}' {container}")
        if not valid_image_id(output):
            raise DockerError("Invalid installed image identity")
        return output

    async def require_owned_id(self, container: str) -> None:
        mounts = await self.checked(
            "docker inspect --type container -f '{{range .Mounts}}{{.Source}} {{end}}' " + container
        )
        if self.directory not in mounts.split():
            raise DockerError("Recovery container does not belong to the panel")

    async def write_journal(self, journal: dict[str, Any]) -> None:
        await self.ssh.write_file(self.journal_path + ".tmp", json.dumps(journal, sort_keys=True))
        await self.checked(f"mv -f {self.journal_path}.tmp {self.journal_path}")

    async def config_snapshot(self) -> None:
        # Do not overwrite a leftover snapshot: it may be the only recovery copy.
        await self.checked(
            f"test ! -e {self.snapshot} && test ! -L {self.directory} && "
            f"mkdir -m 700 {self.snapshot} && cp -a {self.directory}/. {self.snapshot}/ && "
            f"rm -f -- {self.snapshot}/.container-update.json {self.snapshot}/.container-update.json.tmp && "
            f"chmod 700 {self.snapshot}"
        )

    async def restore_snapshot(self) -> None:
        # Keep recovery metadata until the old container has been verified alive again.
        await self.checked(
            f"test -d {self.snapshot} && test ! -L {self.snapshot} && test ! -L {self.directory} && "
            f"find {self.directory} -mindepth 1 -maxdepth 1 "
            "! -name '.container-update.json' ! -name '.container-update-new.cid' "
            "! -name '.container-update.json.tmp' -exec rm -rf -- {} + && "
            f"cp -a {self.snapshot}/. {self.directory}/"
        )

    async def cleanup(self) -> None:
        # Only fixed sibling paths belonging to this layout; never the live config directory.
        await self.checked(
            f"rm -rf -- {self.snapshot} && "
            f"rm -f -- {self.journal_path} {self.journal_path}.tmp {self.cid_path}"
        )

    async def peers(self) -> list[str]:
        output = await self.checked(
            f"docker exec {self.name} awg show {self.layout.interface_name} peers"
        )
        # Only used for equality, never interpolated into shell commands or exposed in a job.
        return sorted(output.split())

    async def ready(self, journal: dict[str, Any], *, preserved_state: bool = True) -> None:
        for attempt in range(10):
            try:
                health = await self.provider.health_check()
                # Exit handshakes depend on traffic and aren't an image-readiness signal.
                checks = [check for check in health.checks if check.name != "exits"]
                if not checks or not all(check.ok for check in checks) or health.port != journal["port"]:
                    raise DockerError("Updated container is not ready")
                if not preserved_state:
                    # Commit is already durable. Clients/routing may legitimately change before
                    # a retry of cleanup; never roll them back to the pre-update snapshot.
                    return
                if await self.peers() != journal["peers"]:
                    raise DockerError("Container peers changed during update")
                if await self.provider._update_runtime_state() != journal["runtime"]:
                    raise DockerError("Gateway routing state was not restored")
                snapshot_present = (await self.ssh.execute(f"test -d {self.snapshot}")).exit_status == 0
                if journal["phase"] != "verified" or snapshot_present:
                    await self.checked(
                        f"diff -qr --exclude=.container-update.json --exclude=.container-update.json.tmp "
                        f"--exclude=.container-update-new.cid {self.snapshot} {self.directory}"
                    )
                return
            except DockerError:
                if attempt == 9:
                    raise
                await asyncio.sleep(0.5)

    def validate_journal(self, raw: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != 1 or data.get("name") != self.name:
                raise ValueError
            if not _CONTAINER_ID.fullmatch(data["old_container_id"]):
                raise ValueError
            if not valid_image_id(data["previous_image_id"]) or not valid_image_id(data["target_image_id"]):
                raise ValueError
            if not _DIGEST.fullmatch(data["target_digest"]):
                raise ValueError
            if type(data["port"]) is not int or not 1 <= data["port"] <= 65535:
                raise ValueError
            if not isinstance(data["peers"], list) or not all(isinstance(peer, str) for peer in data["peers"]):
                raise ValueError
            phases = {"preparing", "prepared", "replacing", "verified"}
            if not isinstance(data["runtime"], dict) or data["phase"] not in phases:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise DockerError("Invalid container update journal; manual recovery is required") from None
        return data

    async def recover(self, journal: dict[str, Any]) -> None:
        old = journal["old_container_id"]
        current, backup = await self.container_id(self.name), await self.container_id(self.backup)
        if backup is not None and backup != old:
            raise DockerError("Unknown backup container; refusing recovery")
        if current == old or backup == old:
            await self.require_owned_id(old)
            if await self.image_id(old) != journal["previous_image_id"]:
                raise DockerError("Original image changed; refusing recovery")
        if journal["phase"] == "preparing":
            # Snapshot may be incomplete, but no container mutation is allowed in this phase.
            if current != old or backup is not None:
                raise DockerError("Container changed during snapshot preparation; refusing recovery")
            await self.cleanup()
            return
        if journal["phase"] == "verified":
            # Commit was recorded before removing the old container; retain the verified replacement.
            cid = await self.ssh.read_file_optional(self.cid_path)
            if not cid or not _CONTAINER_ID.fullmatch(cid.strip()) or current != cid.strip():
                raise DockerError("Verified replacement identity changed; refusing recovery")
            await self.provider._require_own_container()
            if await self.image_id(current) != journal["target_image_id"]:
                raise DockerError("Verified replacement image changed; refusing recovery")
            await self.ready(journal, preserved_state=False)
            if backup:
                await self.checked(f"docker rm {old}")
            await self.cleanup()
            return
        if current == old:
            if backup:
                raise DockerError("Ambiguous old container identity")
        elif backup == old:
            if current:
                cid = await self.ssh.read_file_optional(self.cid_path)
                if not cid or not _CONTAINER_ID.fullmatch(cid.strip()) or current != cid.strip():
                    raise DockerError("Unknown replacement container; refusing recovery")
                await self.provider._require_own_container()
                if await self.image_id(current) != journal["target_image_id"]:
                    raise DockerError("Replacement image changed; refusing recovery")
                await self.checked(f"docker rm -f {current}")
            await self.checked(f"docker rename {old} {self.name}")
        else:
            raise DockerError("Original container is missing; manual recovery is required")
        await self.provider._require_own_container()
        if await self.image_id(old) != journal["previous_image_id"]:
            raise DockerError("Original image changed; refusing recovery")
        # Stop before restoring files: the old process must not race with configuration restore.
        await self.checked(f"docker stop {old}")
        await self.restore_snapshot()
        await self.checked(f"docker start {old}")
        await self.ready(journal)
        await self.cleanup()

    async def run(self) -> ProtocolUpdateResult:
        raw = await self.ssh.read_file_optional(self.journal_path)
        if raw is not None:
            journal = self.validate_journal(raw)
            self.result.previous_image_id = journal["previous_image_id"]
            self.result.target_image_id = journal["target_image_id"]
            self.result.target_digest = journal["target_digest"]
            try:
                await self.recover(journal)
            except Exception:
                self.result.rollback = "failed"
                raise ContainerUpdateError(
                    "Interrupted container update could not be recovered", asdict(self.result)
                ) from None
            self.result.recovered = True
            self.result.rollback = "not_needed"
        await self.provider._require_own_container()
        if await self.container_id(self.backup):
            raise DockerError("Unknown backup container; refusing update")
        state = await self.checked(self.provider._ps("State"))
        if state != "running" or not await self.provider._config_exists():
            raise DockerError("An installed and running panel container is required")
        old = await self.container_id(self.name)
        if old is None:
            raise DockerError("Container disappeared before update")
        try:
            self.result.previous_image_id = await self.image_id(old)
            await self.checked(f"docker pull {AMNEZIAWG_UPDATE_IMAGE}", timeout=300)
            target = await self.checked(f"docker image inspect --format '{{{{.Id}}}}' {AMNEZIAWG_UPDATE_IMAGE}")
            if not valid_image_id(target):
                raise DockerError("Invalid target image identity")
            digests = json.loads(await self.checked(
                f"docker image inspect --format '{{{{json .RepoDigests}}}}' {target}"
            ))
            if not isinstance(digests, list):
                raise DockerError("Invalid image digest metadata")
            valid = [digest for digest in digests if isinstance(digest, str) and _DIGEST.fullmatch(digest)]
            if not valid:
                raise DockerError("No trusted immutable image digest was returned")
            # One image can legitimately have both index and platform RepoDigests.
            # They identify identical image content; verify the selected immutable reference
            # resolves to the downloaded image before replacing any container.
            digest = sorted(set(valid))[0]
            self.result.target_image_id, self.result.target_digest = target, digest
            if target == self.result.previous_image_id:
                return self.result
            installed_image = await self.provider.image_info()
            if installed_image.manifest_digest is not None:
                platform = f"{installed_image.os}/{installed_image.architecture}"
                if installed_image.variant:
                    platform += f"/{installed_image.variant}"
                target_manifest = await self.checked(
                    f"docker image inspect --platform {shlex.quote(platform)} --format '{{{{.Id}}}}' {target}"
                )
                if not valid_image_id(target_manifest):
                    raise DockerError("Invalid target platform manifest")
                if installed_image.manifest_digest == target_manifest:
                    return self.result
            pinned = await self.checked(f"docker image inspect --format '{{{{.Id}}}}' {digest}")
            if pinned != target:
                raise DockerError("Pinned image does not match the downloaded update")
            config = await self.ssh.read_file(self.layout.host_config_file)
            port = int(interface_values(config)["ListenPort"])
            if not 1 <= port <= 65535:
                raise ValueError
            journal = {
                "version": 1, "name": self.name, "old_container_id": old,
                "previous_image_id": self.result.previous_image_id,
                "target_image_id": target, "target_digest": digest, "port": port,
                "peers": await self.peers(), "runtime": await self.provider._update_runtime_state(),
                "phase": "preparing",
            }
        except Exception:
            raise ContainerUpdateError(
                "Could not prepare container update; VPN was not stopped", asdict(self.result)
            ) from None
        # Only claim fixed paths after verifying they aren't leftovers from another operation.
        await self.checked(f"test ! -e {self.snapshot} && test ! -L {self.snapshot} && test ! -e {self.cid_path}")
        await self.write_journal(journal)
        try:
            await self.config_snapshot()
            journal["phase"] = "prepared"
            await self.write_journal(journal)
            # Identity and ownership are rechecked after the slow pull and immediately before stop.
            await self.provider._require_own_container()
            if await self.container_id(self.name) != old:
                raise DockerError("Container changed during update preparation")
            if await self.image_id(old) != self.result.previous_image_id:
                raise DockerError("Container image changed during update preparation")
            journal["phase"] = "replacing"
            await self.write_journal(journal)
            await self.checked(f"docker stop {old}")
            await self.checked(f"docker rename {old} {self.backup}")
            command = self.provider._docker_run_command(port).replace(
                AMNEZIAWG_IMAGE, f"--cidfile {self.cid_path} {digest}", 1
            )
            await self.checked(command, timeout=300)
            new = await self.container_id(self.name)
            cid = await self.ssh.read_file_optional(self.cid_path)
            if not new or not cid or new != cid.strip() or await self.image_id(new) != target:
                raise DockerError("Replacement identity does not match the selected image")
            await self.ready(journal)
            committed = {**journal, "phase": "verified"}
            await self.write_journal(committed)
            journal = committed
            self.result.changed = True
        except Exception:
            try:
                await self.recover(journal)
                self.result.rollback = "succeeded"
            except Exception:
                self.result.rollback = "failed"
            raise ContainerUpdateError("Container update failed", asdict(self.result)) from None
        # A cleanup failure must not roll back an already committed, verified replacement.
        try:
            await self.checked(f"docker rm {old}")
            await self.cleanup()
        except Exception:
            raise ContainerUpdateError(
                "Container updated; recovery metadata cleanup is pending", asdict(self.result)
            ) from None
        return self.result
