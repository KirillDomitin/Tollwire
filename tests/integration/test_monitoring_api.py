"""Мониторинг через HTTP: замер (фейковый SSH) → график за период и сводка последних замеров."""

from uuid import UUID, uuid4

import httpx

from modules.monitoring.service import MetricsCollector


async def test_metrics_endpoints(api: httpx.AsyncClient) -> None:
    server = (await api.post("/api/v1/servers", json={"name": "mon-1", "host": "203.0.113.30"})).json()
    assert (await api.put(f"/api/v1/servers/{server['id']}/credentials", json={"secret": "pw"})).status_code == 200
    assert await MetricsCollector().collect(UUID(server["id"]))

    hour = await api.get(f"/api/v1/servers/{server['id']}/metrics", params={"range": "1h"})
    assert hour.status_code == 200
    body = hour.json()
    assert body["range"] == "1h" and body["resolution"] == "minute"
    (point,) = body["points"]
    assert point["mem_total_bytes"] == 1_000_000 * 1024 and point["cpu_percent"] is None  # первый замер — без скорости

    latest = (await api.get("/api/v1/metrics/latest")).json()
    assert [item["server_id"] for item in latest] == [server["id"]]

    assert (await api.get(f"/api/v1/servers/{server['id']}/metrics", params={"range": "2y"})).status_code == 422
    assert (await api.get(f"/api/v1/servers/{uuid4()}/metrics")).status_code == 404
