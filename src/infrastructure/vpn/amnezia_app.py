"""Чужая установка AmneziaVPN (приложение): панель её читает и может отозвать клиента или вернуть его peer.
Установка, удаление и создание клиентов не поддерживаются."""

import ipaddress
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from core.ssh_exception import CommandExecutionError, ConfigChangedError, UnsupportedOperationError
from infrastructure.vpn.amnezia import validate_name
from infrastructure.vpn.awg_config import (
    append_peer,
    discover_peers,
    importable_peers,
    interface_values,
    is_valid_public_key,
    list_peers,
    parse_clients_table,
    parse_latest_handshakes,
    remove_peer,
    render_app_peer_block,
    used_peer_addresses,
)
from infrastructure.vpn.config import ACTIVE_HANDSHAKE_WINDOW_SECONDS, HOST_CONFIG_DIR
from infrastructure.vpn.provider import (
    DiscoveredClient,
    Discovery,
    HealthCheck,
    ImportablePeer,
    ProviderHealth,
    ProviderState,
    ProviderStatus,
    SyncResult,
    VPNClientConfig,
    VPNClientInfo,
    VPNProvider,
)

logger = logging.getLogger("vpn.amnezia_app")

APP_DIR = "/opt/amnezia/awg"
CLIENTS_TABLE = f"{APP_DIR}/clientsTable"
# Промежуточные файлы для `docker cp` лежат на хосте (SFTP, 0600) и удаляются сразу: в конфиге приватный ключ сервера.
STAGE_PARENT = "/opt/vpn-admin"
STAGE_DIR = f"{STAGE_PARENT}/stage"
CLIENTS_TABLE_MAX_CHARS = 1024 * 1024


def _tunnel_interface(config: str) -> ipaddress.IPv4Interface:
    """Первый IPv4-адрес `Address` интерфейса (у приложения это подсеть клиентов, например 10.8.1.0/24)."""
    try:
        for item in interface_values(config)["Address"].split(","):
            candidate = ipaddress.ip_interface(item.strip())
            if isinstance(candidate, ipaddress.IPv4Interface):
                return candidate
    except (KeyError, ValueError):
        pass
    raise CommandExecutionError("Server config has no valid IPv4 interface address")


class AmneziaAppProvider(VPNProvider):
    """Установка приложения AmneziaVPN: контейнер и конфиг читаются через docker exec. Меняются только peer'ы
    (`revoke_client`, `restore_peer`) через `_edit_config`: перечитывание, откат, выравнивание интерфейса.
    Остальные изменяющие операции — UnsupportedOperationError."""

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

    async def restore_client(self, name: str, client_config: str) -> bool:
        raise UnsupportedOperationError("The panel does not change clients of the AmneziaVPN application")

    # --- запись в контейнер приложения (единственное место, где панель меняет чужой сервер) ---

    async def _require_running(self) -> None:
        found = await self._container()
        if found is None:
            raise CommandExecutionError(f"Container '{self.container}' is not an AmneziaVPN application container")
        if found[0] != ProviderState.RUNNING:
            raise CommandExecutionError(f"Container '{self.container}' is not running")

    async def _put_file(self, stage_name: str, target: str, content: str) -> None:
        """Кладёт файл в контейнер: SFTP на хост -> `docker cp` -> удаление файла хоста при любом исходе."""
        stage = f"{STAGE_DIR}/{self.container}.{stage_name}"
        # Каталог закрытый (0700): в промежуточных файлах бывает приватный ключ сервера. Остатки прерванной
        # операции (в том числе временные файлы SFTP) убираем до записи: сам rm -f ниже знает только своё имя.
        await self.ssh_client.execute(f"mkdir -p {STAGE_PARENT}")
        await self.ssh_client.ensure_private_dir(STAGE_DIR)
        await self.ssh_client.execute(f"rm -f {STAGE_DIR}/{self.container}.*")
        try:
            await self.ssh_client.write_file(stage, content)
            copied = await self.ssh_client.execute(f"docker cp {stage} {self.container}:{target}")
            if copied.exit_status != 0:
                # stderr не берём (как и в _apply): сообщение уходит в результат задачи и в логи.
                raise CommandExecutionError(f"Failed to copy a file into the container (exit {copied.exit_status})")
        finally:
            await self.ssh_client.execute(f"rm -f {stage}")

    async def _apply(self) -> None:
        # syncconf применяет конфиг к живому интерфейсу без перезапуска. В отличие от вызова самого приложения
        # (`syncconf <(strip)`), результат strip сначала проверяется: если strip упал, `<(...)` даёт пустой конфиг,
        # а syncconf с пустым конфигом молча отвечает «успех» и удаляет ВСЕХ peer'ов с живого интерфейса.
        tool = self.tool
        script = (
            f'c=$({tool}-quick strip {self.config_file}) && [ -n "$c" ] && '
            f'{tool} syncconf {self.interface} <(printf "%s\\n" "$c")'
        )
        result = await self.ssh_client.execute(f"docker exec {self.container} bash -c '{script}'")
        if result.exit_status != 0:
            # stderr не берём: awg/wg цитирует строку конфига с ошибкой, а там может быть PrivateKey.
            raise CommandExecutionError(f"Failed to apply the config in '{self.container}' (exit {result.exit_status})")

    async def _edit_config(self, change: Callable[[str], str | None]) -> bool:
        """Читает конфиг, применяет change (None - менять нечего), перечитывает (не менял ли файл тем временем
        сам AmneziaVPN), пишет и применяет; сбой применения откатывает файл. Вызывающий держит ServerLock."""
        await self._require_running()
        original = await self._cat(self.config_file)
        if original is None:
            raise CommandExecutionError(f"Config of '{self.container}' is not readable")
        updated = change(original)
        if updated is None:
            # Файл уже в нужном состоянии, но прошлая операция могла оборваться между записью файла и применением:
            # выравниваем живой интерфейс по файлу (syncconf идемпотентен и клиентов не рвёт).
            await self._apply()
            return False
        if await self._cat(self.config_file) != original:
            raise ConfigChangedError(f"Config of '{self.container}' changed on the server while the panel edited it")
        await self._put_file("conf", self.config_file, updated)
        try:
            await self._apply()
        except BaseException:  # в том числе CancelledError: файл не должен остаться новым при старом интерфейсе
            await self._rollback(original, updated)
            raise
        return True

    async def _rollback(self, original: str, updated: str) -> None:
        """Возвращает прежний конфиг, если за время неудачного применения его не изменило приложение Amnezia:
        чужую правку откатом не затираем. После отката интерфейс выравнивается по файлу (best effort)."""
        try:
            if await self._cat(self.config_file) != updated:
                logger.error("config changed during a failed apply, rollback skipped", extra={"c": self.container})
                return
            await self._put_file("conf", self.config_file, original)
            await self._apply()
        except Exception:
            logger.error("config rollback failed", extra={"container": self.container})

    async def _edit_clients_table(self, change: Callable[[list[object]], list[object] | None]) -> None:
        """Имена клиентов для приложения. Таблица - удобство, доступ важнее: любая проблема с ней не ошибка."""
        try:
            text = await self._cat(CLIENTS_TABLE)
            if text is None or len(text) > CLIENTS_TABLE_MAX_CHARS:
                return
            data = json.loads(text)
            if not isinstance(data, list):
                return  # старый формат (объект) не переписываем
            updated = change(data)
            if updated is None or updated == data:
                return
            payload = json.dumps(updated, indent=4) + "\n"
            await self._put_file("clientsTable", CLIENTS_TABLE, payload)
        except Exception as exc:
            logger.warning(
                "clients table not updated", extra={"container": self.container, "error": type(exc).__name__}
            )

    async def revoke_client(self, public_key: str) -> None:
        """Убирает peer клиента из контейнера приложения и его запись из clientsTable.
        Идемпотентно: нет peer'а - не ошибка."""
        if not is_valid_public_key(public_key):
            raise ValueError("Invalid WireGuard public key")

        def drop(config: str) -> str | None:
            updated, removed = remove_peer(config, public_key)
            return updated if removed else None

        await self._edit_config(drop)
        await self._edit_clients_table(
            lambda table: [e for e in table if not (isinstance(e, dict) and e.get("clientId") == public_key)]
        )

    async def restore_peer(self, name: str, public_key: str, psk: str, address: str) -> bool:
        """Возвращает на сервер peer подхваченного клиента (приватного ключа у панели нет - он только в приложении
        клиента). True - добавлен, False - уже был. Имя проверяется validate_name; в clientsTable оно пишется,
        только если записи о ключе там ещё нет."""
        if not (is_valid_public_key(public_key) and is_valid_public_key(psk)):
            raise ValueError("Invalid WireGuard key")
        validate_name(name)
        try:
            client_address = ipaddress.IPv4Address(address)
        except ValueError:
            raise ValueError("Invalid client address") from None

        def add(config: str) -> str | None:
            if any(peer.public_key == public_key for peer in list_peers(config)):
                return None
            interface = _tunnel_interface(config)
            network = interface.network
            if client_address not in network or client_address in (interface.ip, network.network_address):
                raise ValueError("Client address is outside the tunnel subnet")
            if client_address == network.broadcast_address:
                raise ValueError("Client address is the broadcast address")
            if client_address in used_peer_addresses(config):
                raise CommandExecutionError(f"Address {client_address} is already taken by another peer")
            return append_peer(config, render_app_peer_block(public_key, psk, str(client_address)))

        if not await self._edit_config(add):
            return False
        entry = {"clientId": public_key, "userData": {"clientName": name}}
        await self._edit_clients_table(
            lambda table: (
                table
                if any(isinstance(e, dict) and e.get("clientId") == public_key for e in table)
                else [*table, entry]
            )
        )
        return True

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

    async def read_importable(self) -> list[ImportablePeer]:
        """Всех peer'ов конфига с PSK и именем из clientsTable. Только чтение; остановленный контейнер, не
        контейнер приложения или нечитаемый конфиг - CommandExecutionError (не «клиентов нет»). Имя - удобство:
        нечитаемая таблица даёт клиентов без имён. Сообщения ошибок без содержимого конфига."""
        await self._require_running()
        config = await self._cat(self.config_file)
        if config is None:
            raise CommandExecutionError(f"Config of '{self.container}' is not readable")
        table_text = await self._cat(CLIENTS_TABLE)
        names = parse_clients_table(table_text) if table_text is not None else {}
        return [
            ImportablePeer(p.public_key, p.address, p.psk, names.get(p.public_key), p.supported)
            for p in importable_peers(config)
        ]

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
