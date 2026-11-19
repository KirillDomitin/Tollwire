import httpx
import pytest

SERVERS = "/api/v1/servers"
SERVER = {"name": "test-exit", "host": "203.0.113.10"}


async def test_server_location_is_optional_but_returned_in_the_api(api: httpx.AsyncClient) -> None:
    created = await api.post(SERVERS, json=SERVER)
    assert created.status_code == 201
    assert created.json()["location_city_id"] is None


async def test_server_location_is_persisted_and_can_be_cleared(api: httpx.AsyncClient) -> None:
    created = await api.post(SERVERS, json={**SERVER, "location_city_id": "se-stockholm"})
    assert created.status_code == 201
    assert created.json()["location_city_id"] == "se-stockholm"
    server_id = created.json()["id"]
    url = f"{SERVERS}/{server_id}"

    renamed = await api.patch(url, json={"name": "renamed-exit"})
    assert renamed.status_code == 200
    assert renamed.json()["location_city_id"] == "se-stockholm"
    assert (await api.get(url)).json()["location_city_id"] == "se-stockholm"
    assert (await api.get(SERVERS)).json()[0]["location_city_id"] == "se-stockholm"

    relocated = await api.patch(url, json={"location_city_id": "fi-helsinki"})
    assert relocated.status_code == 200
    assert relocated.json()["location_city_id"] == "fi-helsinki"

    cleared = await api.patch(url, json={"location_city_id": None})
    assert cleared.status_code == 200
    assert (await api.get(url)).json()["location_city_id"] is None

    created_logs = (
        await api.get("/api/v1/audit-logs", params={"entity_id": server_id, "action": "server_created"})
    ).json()
    assert [log["metadata"]["location_city_id"] for log in created_logs] == ["se-stockholm"]
    logs = (await api.get("/api/v1/audit-logs", params={"entity_id": server_id, "action": "server_updated"})).json()
    changes = [log["metadata"]["changes"] for log in logs]
    assert {"location_city_id": ["se-stockholm", "fi-helsinki"]} in changes
    assert {"location_city_id": ["fi-helsinki", None]} in changes


@pytest.mark.parametrize("invalid", ["", "Stockholm", "se/stockholm", "se-stockholm-", "se-" + "x" * 62])
async def test_server_location_rejects_malformed_identifiers(api: httpx.AsyncClient, invalid: str) -> None:
    location = {"location_city_id": invalid}
    assert (await api.post(SERVERS, json={**SERVER, **location})).status_code == 422
    created = await api.post(SERVERS, json=SERVER)
    assert created.status_code == 201
    assert (await api.patch(f"{SERVERS}/{created.json()['id']}", json=location)).status_code == 422
