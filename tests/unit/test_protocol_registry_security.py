"""Trusted metadata HTTP boundary: untrusted registry responses must not redirect or expand access."""

from typing import Any

import httpx
import pytest

from infrastructure.vpn import provider
from tests.fakes.protocol_update import OLD_IMAGE
from tests.unit.test_protocol_update_discovery import index, manifest_metadata, registry_module


def installed_image() -> Any:
    image_cls = getattr(provider, "ProviderImageInfo", None)
    assert image_cls is not None
    return image_cls(image_id=OLD_IMAGE, os="linux", architecture="amd64")


@pytest.mark.parametrize(
    "realm", ["https://attacker.example/token", "http://auth.docker.io/token", "https://auth.docker.io.evil/token"]
)
async def test_bearer_challenge_cannot_send_token_request_to_untrusted_realm(realm: str) -> None:
    registry = registry_module()
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert request.url.host == "registry-1.docker.io"
        return httpx.Response(
            401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer realm="{realm}",service="registry.docker.io",'
                    'scope="repository:amneziavpn/amneziawg-go:pull"'
                )
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(registry.RegistryError) as error:
            await registry.AmneziaImageRegistry(client=client).latest_for(installed_image())
    assert error.value.reason == "registry_unavailable"
    assert len(requests) == 1


async def test_metadata_redirect_is_rejected_even_when_injected_client_follows_redirects() -> None:
    registry = registry_module()
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert request.url.host == "registry-1.docker.io", "must reject redirect before contacting its target"
        return httpx.Response(302, headers={"Location": "https://attacker.example/manifest"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(registry.RegistryError):
            await registry.AmneziaImageRegistry(client=client).latest_for(installed_image())
    assert len(requests) == 1


async def test_oversized_metadata_is_rejected_with_safe_fixed_reason() -> None:
    registry = registry_module()
    body = b"x" * (registry.MAX_METADATA_BYTES + 1)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
        with pytest.raises(registry.RegistryError) as error:
            await registry.AmneziaImageRegistry(client=client).latest_for(installed_image())
    assert error.value.reason == "registry_unavailable"
    assert "xxxxx" not in str(error.value)


async def test_rate_limit_is_not_retried_or_reported_as_available() -> None:
    registry = registry_module()
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(registry.RegistryError) as error:
            await registry.AmneziaImageRegistry(client=client).latest_for(installed_image())
    assert error.value.reason == "rate_limited" and len(requests) == 1


async def test_authentication_retries_metadata_once_using_only_fixed_read_scope() -> None:
    registry = registry_module()
    requests: list[httpx.Request] = []
    body, child_digest = manifest_metadata()
    document = index()
    document["manifests"][0]["digest"] = child_digest

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "auth.docker.io":
            assert request.url.path == "/token"
            assert request.url.params["scope"] == "repository:amneziavpn/amneziawg-go:pull"
            assert request.url.params["service"] == "registry.docker.io"
            return httpx.Response(200, json={"token": "unit-only-registry-token"})
        assert request.url.host == "registry-1.docker.io"
        if request.headers.get("Authorization") != "Bearer unit-only-registry-token":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",'
                        'scope="repository:amneziavpn/amneziawg-go:pull"'
                    )
                },
            )
        if request.url.path.endswith("/latest"):
            return httpx.Response(200, json=document)
        assert request.url.path.endswith(child_digest)
        return httpx.Response(
            200,
            content=body,
            headers={"Docker-Content-Digest": child_digest},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        latest = await registry.AmneziaImageRegistry(client=client).latest_for(installed_image())
    assert latest.image_id == OLD_IMAGE
    assert sum(request.url.host == "auth.docker.io" for request in requests) == 1
    assert sum(request.url.path.endswith("/latest") for request in requests) == 2
