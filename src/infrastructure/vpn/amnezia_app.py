"""Чужая установка AmneziaVPN (приложение): панель её только ЧИТАЕТ."""

from datetime import datetime, timezone

from core.ssh_exception import CommandExecutionError, UnsupportedOperationError
from infrastructure.vpn.awg_config import (
    discover_peers,
    interface_values,
    list_peers,
    parse_clients_table,
    parse_latest_handshakes,
)
from infrastructure.vpn.config import ACTIVE_HANDSHAKE_WINDOW_SECONDS, HOST_CONFIG_DIR
from infrastructure.vpn.provider import (
    DiscoveredClient,
    Discovery,
    HealthCheck,
    ProviderHealth,
    ProviderState,
    ProviderStatus,
    SyncResult,
    VPNClientConfig,
    VPNClientInfo,
    VPNProvider,
)

APP_DIR = "/opt/amnezia/awg"
CLIENTS_TABLE = f"{APP_DIR}/clientsTable"


class AmneziaAppProvider(VPNProvider):
    """Установка приложения AmneziaVPN: контейнер и конфиг читаются через docker exec, состояние сервера не меняется.
    Изменяющие операции — UnsupportedOperationError."""

    container: str
    tool: str
    interface: str
    config_file: str

    async def _container(self) -> tuple[ProviderState, str, str] | None:
        result = await self.ssh_client.execute(
            f"docker ps -a --filter 'name=^/?{self.container}$' "
            "--format '{{.State}}|{{.Image}}|{{.Status}}'"
        )
        if result.exit_status != 0:
            raise CommandExecutionError(f"Failed to list containers: {result.stderr.strip()}")
        line = result.stdout.strip()
        if not line:
            return None
        state, image, status = (part.replace("\x00", "") for part in (line.split("|", 2) + ["", ""])[:3])
        mounts = await self.ssh_client.execute(
            f"docker inspect --type container -f '{{{{range .Mounts}}}}{{{{.Source}}}} {{{{end}}}}' {self.container}"
        )
        if mounts.exit_status != 0:
            raise CommandExecutionError(f"Failed to inspect container '{self.container}'")
        if HOST_CONFIG_DIR in mounts.stdout.split():
            return None  # контейнер панели, а не приложения
        return (ProviderState.RUNNING if state.strip().lower() == "running" else ProviderState.STOPPED), image, status

    async def _cat(self, path: str) -> str | None:
        result = await self.ssh_client.execute(f"docker exec {self.container} cat {path}")
        return result.stdout if result.exit_status == 0 else None

    async def _handshakes(self) -> dict[str, datetime | None] | None:
        result = await self.ssh_client.execute(
            f"docker exec {self.container} {self.tool} show {self.interface} latest-handshakes"
        )
        return parse_latest_handshakes(result.stdout) if result.exit_status == 0 else None

    @staticmethod
    def _port(config: str | None) -> int | None:
        if config is None:
            return None
        try:
            port = int(interface_values(config)["ListenPort"])
        except (KeyError, ValueError):
            return None
        return port if 0 < port < 65536 else None

    async def get_status(self) -> ProviderStatus:
        found = await self._container()
        if found is None:
            return ProviderStatus(ProviderState.NOT_INSTALLED, None, None, None)
        state, image, status = found
        port = self._port(await self._cat(self.config_file)) if state == ProviderState.RUNNING else None
        return ProviderStatus(state, image or None, port, status or None)

    async def install(self) -> ProviderStatus:
        raise UnsupportedOperationError("Installation over an AmneziaVPN application container is not supported")

    async def uninstall(self) -> ProviderStatus:
        raise UnsupportedOperationError("The panel does not remove containers of the AmneziaVPN application")

    async def create_client(self, name: str) -> VPNClientConfig:
        raise UnsupportedOperationError("The panel does not change clients of the AmneziaVPN application")

    async def revoke_client(self, public_key: str) -> None:
        raise UnsupportedOperationError("The panel does not change clients of the AmneziaVPN application")

    async def restore_client(self, name: str, client_config: str) -> bool:
        raise UnsupportedOperationError("The panel does not change clients of the AmneziaVPN application")

    async def _read(self) -> tuple[ProviderStatus, list[DiscoveredClient]] | None:
        found = await self._container()
        if found is None:
            return None
        state, image, status = found
        if state != ProviderState.RUNNING:
            return ProviderStatus(state, image or None, None, status or None), []
        config = await self._cat(self.config_file)
        port = self._port(config)
        if config is None:
            return ProviderStatus(state, image or None, None, status or None), []
        table_text = await self._cat(CLIENTS_TABLE)
        names = parse_clients_table(table_text) if table_text is not None else {}
        handshakes = await self._handshakes() or {}
        clients = [
            DiscoveredClient(
                p.public_key, p.address, names.get(p.public_key), handshakes.get(p.public_key), p.supported
            )
            for p in discover_peers(config)
        ]
        return ProviderStatus(state, image or None, port, status or None), clients

    @staticmethod
    def _infos(clients: list[DiscoveredClient]) -> list[VPNClientInfo]:
        return [VPNClientInfo(c.name, c.public_key, c.address, c.latest_handshake) for c in clients]

    async def list_clients(self) -> list[VPNClientInfo]:
        read = await self._read()
        return [] if read is None else self._infos(read[1])

    async def sync(self) -> SyncResult:
        read = await self._read()
        if read is None:
            return SyncResult(ProviderStatus(ProviderState.NOT_INSTALLED, None, None, None), [])
        return SyncResult(read[0], self._infos(read[1]))

    async def discover(self) -> Discovery | None:
        read = await self._read()
        if read is None:
            return None
        status, clients = read
        return Discovery(self.container, status.server_status, status.version, status.port, clients)

    async def health_check(self) -> ProviderHealth:
        found = await self._container()
        if found is None:
            return ProviderHealth(False, [HealthCheck("container", False, "container not found")], None, 0, 0)
        state, _, _ = found
        checks = [HealthCheck("container", state == ProviderState.RUNNING, state.value)]
        if state != ProviderState.RUNNING:
            return ProviderHealth(True, checks, None, 0, 0)
        config = await self._cat(self.config_file)
        port = self._port(config)
        config_ok = config is not None and port is not None
        checks.append(HealthCheck("config", config_ok, "config readable" if config_ok else "config unreadable"))
        handshakes = await self._handshakes()
        checks.append(HealthCheck("interface", handshakes is not None, f"{self.interface} is up"))
        clients = list_peers(config) if config else []
        now = datetime.now(timezone.utc)
        active = sum(
            1
            for p in clients
            if (h := (handshakes or {}).get(p.public_key))
            and (now - h).total_seconds() <= ACTIVE_HANDSHAKE_WINDOW_SECONDS
        )
        return ProviderHealth(True, checks, port, len(clients), active)


class AmneziaAppAWGProvider(AmneziaAppProvider):
    container = "amnezia-awg"
    tool = "wg"
    interface = "wg0"
    config_file = f"{APP_DIR}/wg0.conf"


class AmneziaAppAWG2Provider(AmneziaAppProvider):
    container = "amnezia-awg2"
    tool = "awg"
    interface = "awg0"
    config_file = f"{APP_DIR}/awg0.conf"
