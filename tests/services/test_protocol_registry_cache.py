"""Different check workers share registry metadata, coalescing requests with Redis locks."""

import asyncio

import httpx

from core.redis import get_redis
from tests.fakes.protocol_update import OLD_IMAGE
from tests.unit.test_protocol_registry_security import installed_image
from tests.unit.test_protocol_update_discovery import index, manifest_metadata, registry_module


async def test_concurrent_registry_instances_share_metadata_cache() -> None:
    registry = registry_module()
    requests: list[str] = []
    body, child_digest = manifest_metadata()
    document = index()
    document["manifests"][0]["digest"] = child_digest

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/latest"):
            return httpx.Response(200, json=document)
        assert request.url.path.endswith(child_digest)
        return httpx.Response(
            200,
            content=body,
            headers={"Docker-Content-Digest": child_digest},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        first = registry.AmneziaImageRegistry(client=client, redis=get_redis())
        second = registry.AmneziaImageRegistry(client=client, redis=get_redis())
        results = await asyncio.gather(first.latest_for(installed_image()), second.latest_for(installed_image()))
        await registry.AmneziaImageRegistry(client=client, redis=get_redis()).latest_for(installed_image())
    assert [item.image_id for item in results] == [OLD_IMAGE, OLD_IMAGE]
    assert sum(path.endswith("/latest") for path in requests) == 1
    assert sum(path.endswith(child_digest) for path in requests) == 1
