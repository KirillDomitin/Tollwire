#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Провайдер «шлюза»: тот же AmneziaWG, но свой контейнер, папка, интерфейс и подсеть (см. gateway_config.py)."""

import contextlib
import logging
import posixpath
from dataclasses import dataclass, field
from typing import ClassVar

from core.ssh_exception import CommandExecutionError
from infrastructure.vpn.amnezia import AmneziaWGProvider
from infrastructure.vpn.awg_config import (
    generate_secret_keys,
    interface_values,
    pick_udp_port,
    render_awg_config,
)
from infrastructure.vpn.config import AMNEZIAWG_IMAGE, RESTART_POLICY, ContainerLayout
from infrastructure.vpn.gateway_config import (
    AWG_SHOW_INTERFACES,
    DOCKER_PUBLISHED_PORTS,
    GATEWAY_LAYOUT,
    IP_RULE_SHOW,
    PORT_PICK_ATTEMPTS,
    apply_rules_command,
    container_start_script,
    exit_interfaces,
    has_kill_switch,
    host_start_script,
    parse_published_ports,
    render_start_script,
    sysctl_flags,
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

    async def _repair(self, port: int) -> None:
        """Повторная установка: чинит то, что отстало от кода панели, не трогая ключи, порт и клиентов."""
        await self._write_start_script()
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

    async def health_check(self) -> ProviderHealth:
        """Проверки обычного протокола и два шлюзовых: правило kill-switch и туннели выходов.

        Пока выходов нет (этап 24.1), `exits` не пройдена: шлюз без выхода клиентам ничего не отдаёт.
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
        interfaces = await self.ssh_client.execute(f"docker exec {layout.container_name} {AWG_SHOW_INTERFACES}")
        exits = exit_interfaces(interfaces.stdout, layout.interface_name) if interfaces.exit_status == 0 else []
        health.checks.append(
            HealthCheck("exits", bool(exits), f"{len(exits)} exit tunnel(s)" if exits else "no exit tunnels")
        )
        return health
