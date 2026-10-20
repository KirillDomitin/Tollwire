#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Провайдер «шлюза»: тот же AmneziaWG, но свой контейнер, папка, интерфейс и подсеть (см. gateway_config.py)."""

import contextlib
import logging
import posixpath
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv4Network
from typing import ClassVar

from core.ssh_exception import CommandExecutionError, DockerError
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.awg_config import (
    generate_secret_keys,
    interface_values,
    parse_latest_handshakes,
    pick_udp_port,
    render_awg_config,
)
from infrastructure.vpn.config import AMNEZIAWG_IMAGE, RESTART_POLICY, ContainerLayout
from infrastructure.vpn.gateway_config import (
    AWG_SHOW_INTERFACES,
    DOCKER_PUBLISHED_PORTS,
    GATEWAY_LAYOUT,
    GATEWAY_SYSCTLS,
    IP_RULE_SHOW,
    PORT_PICK_ATTEMPTS,
    ExitTunnel,
    ExitTunnelStatus,
    RuStatus,
    apply_ru_command,
    apply_ru_script_file,
    apply_rules_command,
    assign_apply_command,
    assign_host_file,
    assign_show_command,
    container_start_script,
    exit_delete_link_command,
    exit_down_command,
    exit_health_label,
    exit_host_dir,
    exit_host_file,
    exit_interface,
    exit_nat_cleanup_command,
    exit_needs_restart,
    exit_show_command,
    exit_slot_from_interface,
    exit_sync_command,
    exit_up_command,
    has_assign_hooks,
    has_kill_switch,
    host_start_script,
    missing_exit_rules,
    parse_assign_chain,
    parse_published_ports,
    parse_transfer,
    prerouting_show_command,
    render_apply_ru_script,
    render_assign_rules,
    render_exit_config,
    render_ru_file,
    render_start_script,
    ru_active_table,
    ru_applied_file,
    ru_prefixes_file,
    ru_routes_command,
    sysctl_flags,
    sysctls_command,
)
from infrastructure.vpn.provider import HealthCheck, ProviderHealth, ProviderStatus

logger = logging.getLogger("vpn.gateway")


@dataclass
class _Created:
    """Что создал текущий вызов install(): откат трогает только это."""

    dir: bool = False
    container: bool = False
    files: list[str] = field(default_factory=list)


class AmneziaGatewayProvider(AmneziaWGProvider):
    layout: ClassVar[ContainerLayout] = GATEWAY_LAYOUT
    # Причина пересоздания контейнера последним install() (None - не пересоздавался). Пересоздание рвёт всех клиентов,
    # поэтому вызывающий сервис пишет его в аудит.
    recreated: str | None = None

    async def install(self) -> ProviderStatus:
        """Идемпотентно: порт и ключи сервера не перегенерируются, start.sh и правила дочиниваются.

        «Первая установка» - только когда нет ни конфига, ни контейнера. Контейнер есть, а конфига нет - ошибка:
        новый конфиг с другим портом не совпал бы с портом работающего контейнера (в БД попал бы порт, на котором шлюз
        не слушает), а чинить такое вслепую нельзя.

        Порядок записи при первой установке: gw0.conf, start.sh, затем `docker run` (контейнер запускает start.sh и
        без него упал бы). Сбой откатывает только то, что создал этот вызов (контейнер, файлы, папку): иначе
        повторная установка нашла бы готовый конфиг с неудачным портом и не выбрала новый.
        """
        await self._require_own_container()
        layout = self.layout
        if await self._config_exists():
            await self._prepare_dir()
            await self._repair(await self._config_port())
            return await self.get_status()
        if (await self.ssh_client.execute(self._ps("State"))).stdout.strip():
            raise CommandExecutionError(
                f"Container '{layout.container_name}' exists but {layout.host_config_file} is missing; "
                "the panel does not guess its port - remove the gateway protocol and install it again"
            )

        created = _Created()
        port = await self._pick_free_port()
        keys = generate_secret_keys()
        config = render_awg_config(
            keys.private_key, port, tunnel_subnet=layout.tunnel_subnet, address=layout.interface_address
        )
        try:
            created.dir = not await self._dir_exists()
            await self._prepare_dir()
            await self.ssh_client.write_file(layout.host_config_file, config)
            created.files.append(layout.host_config_file)
            await self.ssh_client.write_file(host_start_script(layout), render_start_script(layout))
            created.files.append(host_start_script(layout))
            await self.ssh_client.write_file(apply_ru_script_file(layout), render_apply_ru_script(layout))
            created.files.append(apply_ru_script_file(layout))
            created.container = True  # с этой строки docker run мог создать контейнер, даже если вернул ошибку
            await self._run_container(port)
        except BaseException:
            await self._rollback(created)
            raise
        return await self.get_status()

    async def _dir_exists(self) -> bool:
        return (await self.ssh_client.execute(f"test -d {self.layout.host_config_dir}")).exit_status == 0

    async def _prepare_dir(self) -> None:
        """Папка шлюза закрыта для всех, кроме владельца: в ней приватный ключ сервера (0600 у файлов - отдельно)."""
        layout = self.layout
        parent = posixpath.dirname(layout.host_config_dir)
        mkdir = await self.ssh_client.execute(f"mkdir -p {parent}")
        if mkdir.exit_status != 0:
            raise CommandExecutionError(f"Failed to create {parent}: {mkdir.stderr.strip()}")
        await self.ssh_client.ensure_private_dir(layout.host_config_dir)

    async def _config_port(self) -> int:
        try:
            return int(interface_values(await self.ssh_client.read_file(self.layout.host_config_file))["ListenPort"])
        except (KeyError, ValueError) as exc:
            raise CommandExecutionError("ListenPort is missing or invalid in the gateway config") from exc

    async def _pick_free_port(self) -> int:
        """Случайный UDP-порт, не опубликованный ни одним контейнером сервера (в том числе обычным протоколом панели).

        Провайдер видит только свой сервер, поэтому занятые порты читаются с самого сервера константной командой
        `docker ps`, а не берутся из БД: так учитываются и контейнеры, о которых панель не знает. Остановленный
        контейнер порт не показывает - это известное ограничение.
        """
        result = await self.ssh_client.execute(DOCKER_PUBLISHED_PORTS)
        if result.exit_status != 0:
            raise CommandExecutionError(f"Failed to list published ports: {result.stderr.strip()}")
        busy = parse_published_ports(result.stdout)
        for _ in range(PORT_PICK_ATTEMPTS):
            port = pick_udp_port()
            if not any(low <= port <= high for low, high in busy):
                return port
        raise CommandExecutionError("Could not pick a free UDP port for the gateway")

    async def _write_start_script(self) -> None:
        layout = self.layout
        script = render_start_script(layout)
        if await self.ssh_client.read_file_optional(host_start_script(layout)) != script:
            await self.ssh_client.write_file(host_start_script(layout), script)

    async def _write_apply_ru_script(self) -> None:
        layout = self.layout
        script = render_apply_ru_script(layout)
        if await self.ssh_client.read_file_optional(apply_ru_script_file(layout)) != script:
            await self.ssh_client.write_file(apply_ru_script_file(layout), script)

    async def _repair(self, port: int) -> None:
        """Повторная установка: чинит то, что отстало от кода панели, не трогая ключи, порт и клиентов."""
        await self._write_start_script()
        await self._write_apply_ru_script()
        await self._recreate_if_outdated(port)
        state = (await self.ssh_client.execute(self._ps("State"))).stdout.strip()
        if state != "running":
            await self._run_container(port)  # нет контейнера - создаём, остановлен - запускаем
            return
        await self._ensure_nat()
        await self._ensure_restart_policy()
        # Тот же скрипт в режиме apply: правила и недостающие выходы, без перезапуска и без разрыва клиентов.
        result = await self.ssh_client.execute(apply_rules_command(self.layout))
        if result.exit_status != 0:
            raise CommandExecutionError(f"Failed to apply gateway rules: {result.stderr.strip()}")

    async def _recreate_if_outdated(self, port: int) -> None:
        """Контейнер создан с другими sysctl (например, прежним rp_filter=2): sysctl меняются только при создании,
        поэтому контейнер пересоздаётся (`stop && rm`, затем `docker run` с теми же папкой и портом). Клиенты
        переподключатся сами (keepalive); конфиги, ключи и порт на хосте не меняются. Контейнера нет - ничего."""
        layout = self.layout
        if not (await self.ssh_client.execute(self._ps("State"))).stdout.strip():
            return
        result = await self.ssh_client.execute(sysctls_command(layout))
        if result.exit_status != 0:
            raise CommandExecutionError(f"Failed to inspect container '{layout.container_name}'")
        if set(GATEWAY_SYSCTLS) <= set(result.stdout.split()):
            return
        logger.warning("gateway container has outdated sysctls, recreating it")
        name = layout.container_name
        removed = await self.ssh_client.execute(f"docker stop {name} && docker rm {name}")
        if removed.exit_status != 0:
            raise DockerError(f"Failed to remove the outdated container. Error: {removed.stderr.strip()}")
        name = layout.container_name
        self.recreated = "sysctl_mismatch"
        for attempt in (1, 2):  # один повтор: контейнера уже нет, оставлять шлюз без него нельзя
            try:
                await self._run_container(port)
                return
            except DockerError as exc:
                logger.warning("gateway container did not start after recreation", extra={"attempt": attempt})
                if attempt == 2:
                    raise DockerError(
                        f"Container '{name}' was removed to be recreated and could not be started again "
                        f"({str(exc)[:200]}); install the gateway protocol again"
                    ) from exc

    async def _rollback(self, created: "_Created") -> None:
        """Убирает следы неудавшейся первой установки - только созданное этим вызовом. Ошибки отката не заменяют
        исходную: она важнее."""
        layout = self.layout
        if created.container:
            with contextlib.suppress(Exception):
                await self.ssh_client.execute(
                    f"docker stop {layout.container_name} && docker rm {layout.container_name}"
                )
        if created.dir:
            with contextlib.suppress(Exception):
                await self.ssh_client.execute(f"rm -rf {layout.host_config_dir}")
            return
        for path in created.files:  # папка была до нас: убираем только свои файлы
            with contextlib.suppress(Exception):
                await self.ssh_client.execute(f"rm -f {path}")

    def _docker_run_command(self, port: int) -> str:
        layout = self.layout
        return (
            f"docker run -d --name {layout.container_name} --restart {RESTART_POLICY} "
            f"--cap-add=NET_ADMIN --device=/dev/net/tun {sysctl_flags()} "
            f"-p {port}:{port}/udp -v {layout.host_config_dir}:{layout.container_config_dir} "
            f"{AMNEZIAWG_IMAGE} bash {container_start_script(layout)}"
        )

    # --- выходы (этап 24.2) -------------------------------------------------------------------------------------

    async def _require_running(self) -> None:
        """Выходы поднимаются внутри работающего контейнера нашего шлюза; чужой контейнер с этим именем не трогаем."""
        await self._require_own_container()
        state = (await self.ssh_client.execute(self._ps("State"))).stdout.strip()
        if state != "running":
            raise CommandExecutionError(f"Container '{self.layout.container_name}' is not running")

    async def _exit_slots_up(self) -> set[int]:
        """Слоты поднятых туннелей выходов: `awg show interfaces` без клиентского gw0 и чужих имён."""
        result = await self.ssh_client.execute(f"docker exec {self.layout.container_name} {AWG_SHOW_INTERFACES}")
        if result.exit_status != 0:
            raise CommandExecutionError("Failed to list interfaces of the gateway container")
        slots = (exit_slot_from_interface(name) for name in result.stdout.split())
        return {slot for slot in slots if slot is not None}

    async def apply_exits(self, exits: Sequence[ExitTunnel]) -> None:
        """Приводит туннели выходов на шлюзе к заданным: файл `exits/gwx<слот>.conf` и поднятый интерфейс.

        Идемпотентно: тот же конфиг у поднятого интерфейса - ничего не делается; другой ключ/Endpoint - `syncconf` на
        живом интерфейсе; другой Address/Table/MTU - пересоздание. Вызывающий держит ServerLock шлюза. Первый
        упавший туннель прерывает вызов (предыдущие остаются применёнными).
        """
        if not exits:
            return
        await self._require_running()
        await self.ssh_client.ensure_private_dir(exit_host_dir(self.layout))
        up = await self._exit_slots_up()
        for tunnel in exits:
            await self._apply_exit(tunnel, up)

    async def _apply_exit(self, tunnel: ExitTunnel, up: set[int]) -> None:
        """Один туннель. Порядок: (пересоздание: down по СТАРОМУ файлу) -> запись файла -> up / syncconf. Сбой
        возвращает прежнее состояние: у нового выхода файл удаляется, у существующего восстанавливается старый
        конфиг, чтобы файл не оставался без интерфейса, а интерфейс - без файла."""
        layout, slot = self.layout, tunnel.slot
        interface = exit_interface(slot)
        path = exit_host_file(layout, slot)
        new_config = render_exit_config(tunnel)
        old_config = await self.ssh_client.read_file_optional(path)
        is_up = slot in up
        if is_up and old_config == new_config:
            return
        restart = is_up and exit_needs_restart(old_config, new_config)
        if restart:
            await self._exit_down(slot, have_file=old_config is not None)
            up.discard(slot)
        wrote = False
        try:
            await self.ssh_client.write_file(path, new_config)
            wrote = True
            if slot in up:
                await self._exit_run(exit_sync_command(layout, slot), f"Failed to apply the config of {interface}")
            else:
                await self._exit_run(exit_up_command(layout, slot), f"Failed to bring up {interface}")
                up.add(slot)
        except BaseException:
            await self._rollback_exit(slot, old_config, wrote=wrote, restarted=restart, up=up)
            raise

    async def _rollback_exit(
        self, slot: int, old_config: str | None, *, wrote: bool, restarted: bool, up: set[int]
    ) -> None:
        """Возвращает файл и интерфейс как были до вызова. Ошибки отката не заменяют исходную."""
        layout = self.layout
        with contextlib.suppress(Exception):
            if old_config is None:
                if slot in up:
                    await self._exit_down(slot, have_file=wrote)
                    up.discard(slot)
                await self.ssh_client.execute(f"rm -f {exit_host_file(layout, slot)}")
                return
            if wrote:
                await self.ssh_client.write_file(exit_host_file(layout, slot), old_config)
            if restarted:
                await self._exit_run(exit_up_command(layout, slot), "rollback")
                up.add(slot)

    async def _exit_run(self, command: str, error: str) -> None:
        """Константная команда над туннелем. stderr в исключение не идёт: wg/awg цитирует строку конфига с ошибкой, а
        в конфиге приватный ключ."""
        result = await self.ssh_client.execute(command)
        if result.exit_status != 0:
            raise CommandExecutionError(f"{error} (exit code {result.exit_status})")

    async def _exit_down(self, slot: int, *, have_file: bool) -> None:
        """Снять интерфейс: по файлу (отработает PostDown) или, если файла нет, прямым удалением ссылки."""
        layout = self.layout
        interface = exit_interface(slot)
        if have_file:
            await self._exit_run(exit_down_command(layout, slot), f"Failed to bring down {interface}")
        else:
            # Файла нет - PostDown не выполнится: правило NAT снимаем сами, потом ссылку.
            await self.ssh_client.execute(exit_nat_cleanup_command(layout, slot))
            await self._exit_run(exit_delete_link_command(layout, slot), f"Failed to delete {interface}")
        if slot in await self._exit_slots_up():
            raise CommandExecutionError(f"{interface} is still up after the attempt to remove it")

    async def remove_exit(self, slot: int) -> None:
        """Снимает туннель выхода: интерфейс и файл. Идемпотентно (нет интерфейса / файла - не ошибка). Файл удаляется
        только после того, как интерфейс снят: упавший `down` оставляет пару (файл, интерфейс) целой."""
        layout = self.layout
        path = exit_host_file(layout, slot)  # проверяет слот
        await self._require_own_container()
        state = (await self.ssh_client.execute(self._ps("State"))).stdout.strip()
        if state == "running" and slot in await self._exit_slots_up():
            have_file = (await self.ssh_client.execute(f"test -f {path}")).exit_status == 0
            await self._exit_down(slot, have_file=have_file)
        removed = await self.ssh_client.execute(f"rm -f {path}")
        if removed.exit_status != 0:
            raise CommandExecutionError(f"Failed to remove the config of {exit_interface(slot)}")

    async def exit_status(self, expected: Iterable[int] = ()) -> list[ExitTunnelStatus]:
        """Состояние туннелей выходов: поднятые интерфейсы gwx<N> (рукопожатие, счётчики) плюс `expected` - ожидаемые
        слоты (из БД панели), которых нет среди поднятых: для них interface_up=False. Только чтение."""
        layout = self.layout
        up = await self._exit_slots_up()
        statuses = []
        for slot in sorted(up | set(expected)):
            if slot not in up:
                statuses.append(ExitTunnelStatus(slot, False, None))
                continue
            handshakes = await self.ssh_client.execute(exit_show_command(layout, slot, "latest-handshakes"))
            transfer = await self.ssh_client.execute(exit_show_command(layout, slot, "transfer"))
            if handshakes.exit_status != 0:  # интерфейс пропал между перечислением и чтением
                statuses.append(ExitTunnelStatus(slot, False, None))
                continue
            seen = [value for value in parse_latest_handshakes(handshakes.stdout).values() if value is not None]
            rx, tx = parse_transfer(transfer.stdout) if transfer.exit_status == 0 else (0, 0)
            statuses.append(ExitTunnelStatus(slot, True, max(seen) if seen else None, rx, tx))
        return statuses

    # --- назначение выходов клиентам (этап 24.3) ----------------------------------------------------------------

    async def apply_assignments(self, assignments: Mapping[IPv4Address, int]) -> None:
        """Применяет ПОЛНУЮ карту «адрес клиента -> слот выхода» (пустая допустима: у всех клиентов нет выхода, трафик
        режет kill-switch). Файл assign.rules пишется по SFTP (0600) и применяется атомарно (`--test`, затем
        `--noflush`; объявление цепочки очищает её). Сбой возвращает прежний файл и применяет его снова: файл на хосте
        не расходится с цепочкой, которую start.sh загрузит при перезапуске контейнера. Вызывающий держит ServerLock."""
        layout = self.layout
        text = render_assign_rules(assignments, layout.tunnel_subnet)
        await self._require_running()
        if not await self._hooks_present():
            # restore сам создал бы цепочку, а прыжков в неё нет - метки не ставились бы, клиенты остались бы без выхода
            raise CommandExecutionError("The client assignment hooks are missing on the gateway; run install first")
        path = assign_host_file(layout)
        old = await self.ssh_client.read_file_optional(path)
        await self.ssh_client.write_file(path, text)
        try:
            await self._exit_run(assign_apply_command(layout), "Failed to apply the client assignments")
        except BaseException:
            with contextlib.suppress(Exception):
                if old is None:
                    await self.ssh_client.execute(f"rm -f {path}")
                else:
                    await self.ssh_client.write_file(path, old)
                    await self._exit_run(assign_apply_command(layout), "rollback")
            raise

    async def _hooks_present(self) -> bool:
        """Три прыжка start.sh в PREROUTING на месте. Без них метки не ставятся, а само существование цепочки GW_ASSIGN
        ничего не значит: `iptables-restore` создаёт её и сам."""
        result = await self.ssh_client.execute(prerouting_show_command(self.layout))
        return result.exit_status == 0 and has_assign_hooks(result.stdout, self.layout.interface_name)

    async def assignment_problems(self) -> list[str]:
        """Чего не хватает для назначений на работающем шлюзе (только чтение): прыжков PREROUTING и правил `ip rule`
        метки у поднятых выходов. Пусто - всё на месте. Без деталей конфигов."""
        layout = self.layout
        problems = []
        if not await self._hooks_present():
            problems.append("PREROUTING hooks of the client assignments are missing")
        rules = await self.ssh_client.execute(f"docker exec {layout.container_name} {IP_RULE_SHOW}")
        if rules.exit_status != 0:
            problems.append("failed to read ip rules")
            return problems
        for slot in missing_exit_rules(rules.stdout, sorted(await self._exit_slots_up())):
            problems.append(f"ip rule of {exit_interface(slot)} is missing")
        return problems

    async def read_assignments(self) -> dict[IPv4Address, int]:
        """Фактическая карта в цепочке GW_ASSIGN (только чтение). Цепочки нет (шлюз со старым start.sh) -
        CommandExecutionError."""
        await self._require_running()
        result = await self.ssh_client.execute(assign_show_command(self.layout))
        if result.exit_status != 0:
            raise CommandExecutionError("The assignment chain is missing on the gateway")
        return parse_assign_chain(result.stdout)

    # --- Россия напрямую (этап 24.4) ----------------------------------------------------------------------------

    async def apply_ru(self, prefixes: Iterable[IPv4Network]) -> RuStatus:
        """Заменяет российские маршруты шлюза списком `prefixes` (атомарно: запасная таблица 200/201, затем переключение
        `ip rule 100`). Файл `ru.prefixes` (SFTP, 0600) и скрипт `apply-ru.sh` пишутся перед запуском; сбой возвращает
        прежний список в файле (активная таблица скрипт не трогает, так что при перезапуске контейнера загрузится
        тот же список, что работает сейчас). Вызывающий держит ServerLock шлюза. Возвращает итоговое состояние."""
        layout = self.layout
        text, _ = render_ru_file(prefixes)
        if not text:
            raise ValueError("The Russian-direct list is empty")
        await self._require_running()
        script_path, list_path = apply_ru_script_file(layout), ru_prefixes_file(layout)
        script = render_apply_ru_script(layout)
        if await self.ssh_client.read_file_optional(script_path) != script:
            await self.ssh_client.write_file(script_path, script)
        old = await self.ssh_client.read_file_optional(list_path)
        await self.ssh_client.write_file(list_path, text)
        try:
            result = await self.ssh_client.execute(apply_ru_command(layout), timeout=300)
            if result.exit_status != 0:
                raise CommandExecutionError(f"Failed to apply the Russian routes (exit code {result.exit_status})")
        except BaseException:
            with contextlib.suppress(Exception):
                if old is None:
                    await self.ssh_client.execute(f"rm -f {list_path}")
                else:
                    await self.ssh_client.write_file(list_path, old)
            raise
        return await self.ru_status()

    async def ru_status(self) -> RuStatus:
        """Активная таблица `ru`, число маршрутов и отпечаток применённого списка. Нет правила 100 - (None, 0)."""
        layout = self.layout
        rules = await self.ssh_client.execute(f"docker exec {layout.container_name} {IP_RULE_SHOW}")
        if rules.exit_status != 0:
            raise CommandExecutionError("Failed to read ip rules of the gateway")
        table = ru_active_table(rules.stdout, layout.tunnel_subnet)
        if table is None:
            return RuStatus(None, 0)
        routes = await self.ssh_client.execute(ru_routes_command(layout, table))
        if routes.exit_status != 0:
            raise CommandExecutionError("Failed to read the Russian routes of the gateway")
        applied = await self.ssh_client.read_file_optional(ru_applied_file(layout))
        digest = applied.strip() if applied and applied.strip() else None
        return RuStatus(table, sum(1 for line in routes.stdout.splitlines() if line.strip()), digest)

    async def assignments_in_sync(self, desired: Mapping[IPv4Address, int]) -> bool:
        """Карта в цепочке GW_ASSIGN и файл assign.rules (его загрузит start.sh после перезапуска) совпадают с желаемой.
        Цепочки нет - CommandExecutionError (см. read_assignments)."""
        if await self.read_assignments() != dict(desired) or await self.assignment_problems():
            return False
        text = render_assign_rules(desired, self.layout.tunnel_subnet)
        saved = await self.ssh_client.read_file_optional(assign_host_file(self.layout))
        return saved == text or (saved is None and not desired)  # карты нет и не нужна: файла может и не быть

    async def health_check(self) -> ProviderHealth:
        """Проверки обычного протокола и два шлюзовых: правило kill-switch и туннели выходов.

        `exits` пройдена, если есть хотя бы один исправный выход: интерфейс поднят и рукопожатие свежее
        (ACTIVE_HANDSHAKE_WINDOW_SECONDS). В деталях - состояние каждого туннеля по слоту, без ключей. Пока выходов нет,
        `exits` не пройдена: шлюз без выхода клиентам ничего не отдаёт.
        """
        health = await super().health_check()
        container_running = any(check.name == "container" and check.ok for check in health.checks)
        if not container_running:
            return health
        layout = self.layout
        rules = await self.ssh_client.execute(f"docker exec {layout.container_name} {IP_RULE_SHOW}")
        present = rules.exit_status == 0 and has_kill_switch(rules.stdout, layout.tunnel_subnet)
        health.checks.append(
            HealthCheck(
                "kill_switch", present, "prohibit rule present" if present else "no prohibit rule: clients may leak"
            )
        )
        try:
            statuses = await self.exit_status()
        except CommandExecutionError:
            health.checks.append(HealthCheck("exits", False, "failed to list exit tunnels"))
            return health
        now = datetime.now(timezone.utc)
        labels = {status.slot: exit_health_label(status, now) for status in statuses}
        healthy = sum(1 for label in labels.values() if label == "ok")
        if not statuses:
            detail = "no exit tunnels"
        else:
            states = ", ".join(f"{exit_interface(slot)} {label}" for slot, label in labels.items())
            detail = f"{len(statuses)} exit tunnel(s), {healthy} healthy: {states}"
        health.checks.append(HealthCheck("exits", healthy > 0, detail))
        problems = await self.assignment_problems()
        health.checks.append(
            HealthCheck("assignments", not problems, "; ".join(problems) or "marks, hooks and exit rules in place")
        )
        return health
