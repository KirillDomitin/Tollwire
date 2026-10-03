"""API ключа SSH-сервера: scan (без входа), ручное закрепление, сброс, автосброс при смене адреса."""

import asyncssh
import httpx
import pytest

from core.exceptions import SSHTimeoutError
from infrastructure.ssh.host_keys import HostKey
from modules.servers import host_key as host_key_module


def new_key() -> HostKey:
    return HostKey.from_ssh_key(asyncssh.generate_private_key("ssh-ed25519").convert_to_public())


async def make_server(api: httpx.AsyncClient) -> str:
    server = (await api.post("/api/v1/servers", json={"name": "srv", "host": "203.0.113.10"})).json()
    assert server["ssh_host_key_fingerprint"] is None
    return str(server["id"])


async def audit_actions(api: httpx.AsyncClient) -> list[str]:
    return [log["action"] for log in (await api.get("/api/v1/audit-logs")).json()]


async def test_scan_then_pin_then_reset(api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = await make_server(api)
    presented = new_key()

    async def fake_scan(host: str, port: int, timeout: float) -> HostKey:
        assert (host, port) == ("203.0.113.10", 22)
        return presented

    monkeypatch.setattr(host_key_module, "scan_host_key", fake_scan)

    scan = (await api.post(f"/api/v1/servers/{sid}/host-key/scan")).json()
    assert scan == {
        "public_key": presented.public_key,
        "fingerprint": presented.fingerprint,
        "pinned_fingerprint": None,
        "matches_pinned": None,
    }

    # админ сверил отпечаток с консолью хостинга и закрепляет (комментарий в строке допускается)
    pinned = await api.put(f"/api/v1/servers/{sid}/host-key", json={"public_key": f"{presented.public_key} root@x"})
    assert pinned.status_code == 200 and pinned.json()["ssh_host_key_fingerprint"] == presented.fingerprint
    assert (await api.post(f"/api/v1/servers/{sid}/host-key/scan")).json()["matches_pinned"] is True

    reset = await api.delete(f"/api/v1/servers/{sid}/host-key")
    assert reset.status_code == 200 and reset.json()["ssh_host_key_fingerprint"] is None
    actions = await audit_actions(api)
    assert "server_host_key_pinned" in actions and "server_host_key_reset" in actions


async def test_pin_rejects_garbage_and_unknown_server(api: httpx.AsyncClient) -> None:
    sid = await make_server(api)
    bad = await api.put(f"/api/v1/servers/{sid}/host-key", json={"public_key": "ssh-ed25519 definitely-not-a-key"})
    assert bad.status_code == 422
    missing = "00000000-0000-0000-0000-000000000000"
    key = new_key().public_key
    assert (await api.put(f"/api/v1/servers/{missing}/host-key", json={"public_key": key})).status_code == 404
    assert (await api.delete(f"/api/v1/servers/{missing}/host-key")).status_code == 404
    assert (await api.post(f"/api/v1/servers/{missing}/host-key/scan")).status_code == 404


async def test_scan_unreachable_is_502(api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = await make_server(api)

    async def unreachable(host: str, port: int, timeout: float) -> HostKey:
        raise SSHTimeoutError("Host key scan timeout exceeded")

    monkeypatch.setattr(host_key_module, "scan_host_key", unreachable)
    assert (await api.post(f"/api/v1/servers/{sid}/host-key/scan")).status_code == 502


async def test_host_change_resets_pinned_key_and_deletes_credentials(api: httpx.AsyncClient) -> None:
    """Сохранённый root-пароль не должен уйти на новый адрес: учётку и ключ для него задают заново."""
    sid = await make_server(api)
    key = new_key()
    await api.put(f"/api/v1/servers/{sid}/host-key", json={"public_key": key.public_key})
    await api.put(f"/api/v1/servers/{sid}/credentials", json={"secret": "root-password"})

    renamed = (await api.patch(f"/api/v1/servers/{sid}", json={"name": "srv-2"})).json()
    assert renamed["ssh_host_key_fingerprint"] == key.fingerprint  # имя — та же машина, ключ остаётся
    assert (await api.get(f"/api/v1/servers/{sid}/credentials")).status_code == 200  # и учётка тоже

    moved = (await api.patch(f"/api/v1/servers/{sid}", json={"host": "203.0.113.20"})).json()
    assert moved["ssh_host_key_fingerprint"] is None  # другой адрес — другая машина
    assert (await api.get(f"/api/v1/servers/{sid}/credentials")).status_code == 404
    (update,) = [
        log
        for log in (await api.get("/api/v1/audit-logs", params={"action": "server_updated"})).json()
        if "host" in log["metadata"]["changes"]
    ]
    assert update["metadata"]["host_key_reset"] == {"previous": key.fingerprint}
    assert update["metadata"]["credentials_deleted"] is True


async def test_ssh_key_credentials_are_validated(api: httpx.AsyncClient) -> None:
    sid = await make_server(api)
    url = f"/api/v1/servers/{sid}/credentials"
    garbage = await api.put(url, json={"auth_type": "ssh_key", "secret": "/root/.ssh/id_ed25519"})
    assert garbage.status_code == 422  # путь или мусор вместо ключа — отказ сразу, а не при подключении
    key = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
    ok = await api.put(url, json={"auth_type": "ssh_key", "secret": key})
    assert ok.status_code == 200 and ok.json()["auth_type"] == "ssh_key" and "secret" not in ok.text
