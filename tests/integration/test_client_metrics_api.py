"""Client monitoring is an authenticated cache-only read, including empty samples."""

from uuid import uuid4

import httpx

from tests.fixtures import World


async def test_client_monitoring_empty_cache_does_not_contact_the_server(api: httpx.AsyncClient, world: World) -> None:
    server, protocol, fake = await world.server()
    assert protocol is not None
    before = list(fake.commands)
    response = await api.get(f"/api/v1/servers/{server.id}/clients/monitoring")
    assert response.status_code == 200
    assert response.json() == {
        "server_id": str(server.id),
        "protocols": [{"protocol_id": str(protocol.id), "sampled_at": None, "stale": False, "clients": []}],
    }
    assert fake.commands == before


async def test_client_monitoring_validates_server_id_and_unknown_server(api: httpx.AsyncClient) -> None:
    assert (await api.get(f"/api/v1/servers/{uuid4()}/clients/monitoring")).status_code == 404
    assert (await api.get("/api/v1/servers/not-a-uuid/clients/monitoring")).status_code == 422


async def test_client_monitoring_requires_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"/api/v1/servers/{uuid4()}/clients/monitoring")).status_code == 401
