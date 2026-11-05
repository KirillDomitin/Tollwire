"""Read-only image inspection and multiarch registry metadata contracts."""

import importlib
import importlib.util
import json
from hashlib import sha256
from typing import Any

import httpx
import pytest

from core.ssh_exception import DockerError
from infrastructure.ssh.client import CommandResult
from infrastructure.vpn import provider
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.config import DEFAULT_LAYOUT
from infrastructure.vpn.gateway import AmneziaGatewayProvider
from infrastructure.vpn.gateway_config import GATEWAY_LAYOUT
from tests.fakes.fake_ssh import FakeServer, FakeSSHClient
from tests.fakes.protocol_update import INDEX_IMAGE, NEW_IMAGE, OLD_IMAGE

AMD_MANIFEST = "sha256:" + "d" * 64
ARM_MANIFEST = "sha256:" + "e" * 64
REPOSITORY = "amneziavpn/amneziawg-go"


def registry_module() -> Any:
    name = "infrastructure.vpn.registry"
    assert importlib.util.find_spec(name) is not None, "Read-only trusted registry metadata resolver is required"
    return importlib.import_module(name)


def index() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"digest": AMD_MANIFEST, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": ARM_MANIFEST, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
        ],
    }


def manifest_metadata(image_id: str = OLD_IMAGE) -> tuple[bytes, str]:
    document = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": image_id, "size": 123, "mediaType": "application/vnd.oci.image.config.v1+json"},
        "layers": [],
    }
    body = json.dumps(document).encode()
    return body, "sha256:" + sha256(body).hexdigest()


@pytest.mark.parametrize("architecture,variant,digest", [("amd64", None, AMD_MANIFEST), ("arm64", "v8", ARM_MANIFEST)])
def test_multiarch_selects_installed_platform_not_first_manifest(
    architecture: str, variant: str | None, digest: str
) -> None:
    registry = registry_module()
    assert registry.select_platform_manifest(index(), "linux", architecture, variant) == digest


@pytest.mark.parametrize("ambiguous", [False, True])
def test_missing_or_ambiguous_platform_is_unknown_instead_of_guessing(ambiguous: bool) -> None:
    registry = registry_module()
    document = index()
    if ambiguous:
        document["manifests"].append({"digest": ARM_MANIFEST, "platform": {"os": "linux", "architecture": "amd64"}})
    with pytest.raises(ValueError):
        registry.select_platform_manifest(document, "linux", "amd64" if ambiguous else "riscv64")


@pytest.mark.parametrize("gateway", [False, True], ids=["awg", "gateway"])
async def test_image_info_only_inspects_container_and_its_actual_image(gateway: bool) -> None:
    layout = GATEWAY_LAYOUT if gateway else DEFAULT_LAYOUT

    class ImageServer(FakeServer):
        def run(self, command: str) -> CommandResult:
            if "inspect" in command and "{{.Image}}" in command:
                self.commands.append(command)
                return CommandResult(OLD_IMAGE + "\n", "", 0)
            if command.startswith("docker image inspect "):
                self.commands.append(command)
                assert OLD_IMAGE in command, "inspect actual container image ID, not mutable latest tag"
                data = {"Id": OLD_IMAGE, "Os": "linux", "Architecture": "arm64", "Variant": "v8", "RepoDigests": []}
                return CommandResult(json.dumps(data if "{{" in command else [data]), "", 0)
            return super().run(command)

    fake = ImageServer(layout=layout)
    async with FakeSSHClient(fake) as ssh:
        instance = (AmneziaGatewayProvider if gateway else AmneziaWGProvider)(ssh)
        await instance.install()
        fake.commands.clear()
        before = dict(fake.files)
        method = getattr(instance, "image_info", None)
        assert callable(method), "VPNProvider.image_info() must read installed image without pulling"
        info = await method()
    assert (info.image_id, info.os, info.architecture, info.variant) == (OLD_IMAGE, "linux", "arm64", "v8")
    assert fake.files == before and fake.running
    assert not any(
        c.startswith(("docker pull", "docker stop", "docker run", "docker rm", "docker rename")) for c in fake.commands
    )


async def test_registry_compares_platform_config_digest_with_local_image_id() -> None:
    registry = registry_module()
    image_cls = getattr(provider, "ProviderImageInfo", None)
    assert image_cls is not None, "ProviderImageInfo must describe installed architecture"
    requests: list[str] = []
    body, child_digest = manifest_metadata()
    document = index()
    document["manifests"][1]["digest"] = child_digest

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert request.url.host == "registry-1.docker.io"
        if request.url.path.endswith("/manifests/latest"):
            return httpx.Response(
                200, json=document, headers={"Content-Type": "application/vnd.oci.image.index.v1+json"}
            )
        if request.url.path.endswith(f"/manifests/{child_digest}"):
            return httpx.Response(
                200,
                content=body,
                headers={
                    "Content-Type": "application/vnd.oci.image.manifest.v1+json",
                    "Docker-Content-Digest": child_digest,
                },
            )
        raise AssertionError(f"Unexpected metadata request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        image = image_cls(image_id=OLD_IMAGE, os="linux", architecture="arm64", variant="v8")
        latest = await registry.AmneziaImageRegistry(client=client).latest_for(image)
    assert latest.image_id == OLD_IMAGE  # index/child manifest digest differs, but no actual image update exists.
    assert latest.digest == f"{REPOSITORY}@{child_digest}"
    assert not any(AMD_MANIFEST in request for request in requests)
    assert not any("/blobs/" in request for request in requests)  # metadata only; no image layers.


@pytest.mark.parametrize("gateway", [False, True], ids=["awg", "gateway"])
@pytest.mark.parametrize("architecture", ["amd64", "arm64"], ids=["matching-platform", "wrong-platform"])
async def test_containerd_image_info_reads_actual_container_platform_descriptor_without_mutation(
    gateway: bool,
    architecture: str,
) -> None:
    class DescriptorServer(FakeServer):
        def run(self, command: str) -> CommandResult:
            if "{{.Image}}" in command:
                self.commands.append(command)
                return CommandResult(INDEX_IMAGE + "\n", "", 0)
            if command == f"docker image inspect --format '{{{{json .}}}}' {INDEX_IMAGE}":
                self.commands.append(command)
                return CommandResult(json.dumps({"Id": INDEX_IMAGE, "Os": "linux", "Architecture": "amd64"}), "", 0)
            if command == (
                "docker inspect --type container -f '{{json .ImageManifestDescriptor}}' " + self.layout.container_name
            ):
                self.commands.append(command)
                descriptor = {"digest": AMD_MANIFEST, "platform": {"os": "linux", "architecture": architecture}}
                return CommandResult(json.dumps(descriptor), "", 0)
            return super().run(command)

    fake = DescriptorServer(layout=GATEWAY_LAYOUT if gateway else DEFAULT_LAYOUT)
    async with FakeSSHClient(fake) as ssh:
        instance = (AmneziaGatewayProvider if gateway else AmneziaWGProvider)(ssh)
        await instance.install()
        fake.commands.clear()
        before = dict(fake.files)
        if architecture == "arm64":
            with pytest.raises(DockerError):
                await instance.image_info()
        else:
            info = await instance.image_info()
            assert info.image_id == INDEX_IMAGE
            assert getattr(info, "manifest_digest", None) == AMD_MANIFEST
    assert fake.files == before and fake.running
    assert not any(c.startswith(("docker pull", "docker stop", "docker run", "docker rm")) for c in fake.commands)


@pytest.mark.parametrize("target", [OLD_IMAGE, NEW_IMAGE], ids=["same-config-repacked-manifest", "changed-config"])
async def test_containerd_registry_resolves_installed_manifest_config_instead_of_comparing_index_id(
    target: str,
) -> None:
    registry = registry_module()
    image_cls = provider.ProviderImageInfo
    assert "manifest_digest" in image_cls.__dataclass_fields__, "containerd needs its installed platform descriptor"
    installed_body, installed_digest = manifest_metadata(OLD_IMAGE)
    target_document = json.loads(manifest_metadata(target)[0])
    # Different manifests may contain identical config content (for example a repack).
    target_document["annotations"] = {"org.opencontainers.image.revision": "repacked"}
    target_body = json.dumps(target_document).encode()
    target_digest = "sha256:" + sha256(target_body).hexdigest()
    document = index()
    document["manifests"][0]["digest"] = target_digest
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "registry-1.docker.io"
        paths.append(request.url.path)
        if request.url.path.endswith("/manifests/latest"):
            return httpx.Response(200, json=document)
        for digest, body in ((installed_digest, installed_body), (target_digest, target_body)):
            if request.url.path.endswith(f"/manifests/{digest}"):
                return httpx.Response(200, content=body, headers={"Docker-Content-Digest": digest})
        raise AssertionError(f"Unexpected metadata request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        image = image_cls(image_id=INDEX_IMAGE, os="linux", architecture="amd64", manifest_digest=installed_digest)
        latest = await registry.AmneziaImageRegistry(client=client).latest_for(image)
    assert latest.current_image_id == OLD_IMAGE
    assert latest.image_id == target
    assert latest.digest == f"{REPOSITORY}@{target_digest}"
    assert any(path.endswith(f"/manifests/{installed_digest}") for path in paths)
    assert not any("/blobs/" in path for path in paths)
