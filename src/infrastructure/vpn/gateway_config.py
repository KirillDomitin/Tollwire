#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Шлюз: раскладка контейнера и чистые генераторы текста (без SSH).

Шлюз - российский сервер, к которому клиенты подключаются одним конфигом. Контейнер `vpn-admin-gw` завершает клиентский
AmneziaWG и пересылает трафик на зарубежные серверы панели («выходы», этап 24.2), российские адреса - напрямую
(этап 24.4). Здесь только то, что не требует соединения: раскладка, флаги `docker run`, текст `start.sh`, разбор вывода
`ip rule` и `docker ps`.
"""

import hashlib
import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from infrastructure.vpn.awg_config import (
    client_config_sections,
    interface_values,
    is_valid_public_key,
    obfuscation_params,
)
from infrastructure.vpn.config import (
    ACTIVE_HANDSHAKE_WINDOW_SECONDS,
    CLIENT_DNS,
    CLIENT_PERSISTENT_KEEPALIVE,
    ContainerLayout,
)
from infrastructure.vpn.split_routes import MIN_ROUTE_PREFIXLEN, is_routable

GATEWAY_CONTAINER_NAME = "vpn-admin-gw"
GATEWAY_HOST_DIR = "/opt/vpn-admin/gw"
GATEWAY_CONTAINER_DIR = "/etc/vpn-admin/gw"
GATEWAY_CONFIG_FILE = "gw0.conf"
GATEWAY_INTERFACE = "gw0"
GATEWAY_SUBNET = "10.77.0.0/22"
GATEWAY_ADDRESS = "10.77.0.1/22"

# DNS клиентов идёт через туннель и дальше через выход (а не на сам шлюз), поэтому как у обычного протокола.
GATEWAY_LAYOUT = ContainerLayout(
    container_name=GATEWAY_CONTAINER_NAME,
    host_config_dir=GATEWAY_HOST_DIR,
    container_config_dir=GATEWAY_CONTAINER_DIR,
    config_file_name=GATEWAY_CONFIG_FILE,
    interface_name=GATEWAY_INTERFACE,
    tunnel_subnet=GATEWAY_SUBNET,
    client_dns=CLIENT_DNS,
    server_address=GATEWAY_ADDRESS,
)

START_SCRIPT_NAME = "start.sh"
ASSIGN_FILE_NAME = (
    "assign.rules"  # <папка шлюза>/assign.rules - карта «адрес клиента -> метка выхода» (iptables-restore)
)
ASSIGN_CHAIN = "GW_ASSIGN"  # цепочка mangle, в которую панель кладёт по правилу на клиента
EXITS_DIR_NAME = "exits"  # <папка шлюза>/exits/gwx<слот>.conf - туннели к выходам (этап 24.2)

# Приоритеты правил маршрутизации (ip rule) трафика клиентов. Меньше число - раньше проверяется.
RU_TABLE = 200  # таблица «российское напрямую» (этап 24.4); правило 100 указывает на 200 или 201 по очереди
RU_TABLE_SPARE = 201  # запасная: список собирается в ней, затем правило 100 переключается (атомарная замена)
RU_TABLES = (RU_TABLE, RU_TABLE_SPARE)
RU_FILE_NAME = "ru.prefixes"  # <папка шлюза>/ru.prefixes - `# sha256=<hex>` и российские префиксы по CIDR в строке
RU_APPLIED_NAME = "ru.applied"  # <папка шлюза>/ru.applied - отпечаток списка, применённого скриптом успешно
APPLY_RU_SCRIPT_NAME = "apply-ru.sh"  # <папка шлюза>/apply-ru.sh - константный скрипт замены таблицы
RU_RULE_PRIORITY = 100
EXIT_RULE_PRIORITY = 1000  # fwmark выхода -> его таблица (этап 24.3); позже ru (100), раньше prohibit (9000)
KILL_SWITCH_PRIORITY = 9000  # prohibit: трафик клиентов никогда не уходит в main (утечка зарубежного, доступ к хосту)

# sysctl контейнера. ip_forward - пересылка. rp_filter=0 (отключён, этап 24.3): иначе ядро отбрасывает ответы выходов -
# проверка обратного пути ищет маршрут к источнику ответа "от адреса клиента", и такой поиск попадает под kill-switch
# (`from <подсеть клиентов> prohibit`; отличить его от пакета клиента правилами нельзя), поэтому даже в режиме loose (2)
# растёт счётчик IPReversePathFilter, а TCP-ответ до клиента не доходит. Подмена адресов не расширяется: клиент в gw0
# ограничен AllowedIPs (своим /32), выходы - собственные серверы панели. IPv6 выключен целиком: правила шлюза только
# IPv4, через IPv6 клиент утёк бы мимо kill-switch.
GATEWAY_SYSCTLS = (
    "net.ipv4.ip_forward=1",
    "net.ipv4.conf.all.rp_filter=0",
    "net.ipv4.conf.default.rp_filter=0",
    "net.ipv6.conf.all.disable_ipv6=1",
)

# Константные команды внутри контейнера шлюза (в них нет пользовательского ввода).
IP_RULE_SHOW = "ip rule show"
AWG_SHOW_INTERFACES = "awg show interfaces"
# Занятые порты хоста: все контейнеры (в том числе не панели), только чтение.
DOCKER_PUBLISHED_PORTS = "docker ps -a --format '{{.Ports}}'"
PORT_PICK_ATTEMPTS = 100


SYSCTLS_SHOW = "docker inspect -f '{{{{range $k, $v := .HostConfig.Sysctls}}}}{{{{$k}}}}={{{{$v}}}} {{{{end}}}}' {name}"


def sysctls_command(layout: ContainerLayout) -> str:
    """Sysctl, с которыми создан контейнер (`k=v` через пробел): по ним install() узнаёт устаревший контейнер."""
    return SYSCTLS_SHOW.format(name=layout.container_name)


def sysctl_flags() -> str:
    return " ".join(f"--sysctl {item}" for item in GATEWAY_SYSCTLS)


def container_start_script(layout: ContainerLayout) -> str:
    """Путь start.sh внутри контейнера."""
    return f"{layout.container_config_dir}/{START_SCRIPT_NAME}"


def host_start_script(layout: ContainerLayout) -> str:
    """Путь start.sh на хосте (папка шлюза смонтирована в контейнер целиком)."""
    return f"{layout.host_config_dir}/{START_SCRIPT_NAME}"


def apply_rules_command(layout: ContainerLayout) -> str:
    """Дочинить правила в работающем контейнере: тот же скрипт в режиме `apply` (без подъёма gw0 и без ожидания)."""
    return f"docker exec {layout.container_name} bash {container_start_script(layout)} apply"


# Скрипт запуска контейнера. Подстановки - только константы раскладки (не ввод пользователя). Скрипт перезаписывается
# при каждой установке и выполняется при каждом старте контейнера, поэтому каждое действие идемпотентно.
#
# Порядок: правила маршрутизации и изоляция ДО подъёма gw0 (kill-switch действует с первой секунды: пока интерфейса
# нет, клиентам некуда слать, но правило не должно появляться «после» - иначе при любой задержке дальше трафик пошёл бы
# в main), затем gw0 (упал - выход с ошибкой, сработает restart policy), затем выходы (упавший выход контейнер не
# роняет), затем подстройка MSS. Режим `apply` - то же самое для уже работающего контейнера, без gw0 и tail.
_START_SCRIPT = r"""#!/bin/bash
# Generated by the VPN panel and rewritten on every install - do not edit.
# start.sh        - container start: rules, client interface, exit tunnels, then stay alive
# start.sh apply  - running container: re-apply the idempotent rules and bring up missing exit tunnels
set -eu

GW_DIR=@CONTAINER_DIR@
GW_IF=@INTERFACE@
GW_CONF="$GW_DIR/@CONFIG_FILE@"
SUBNET=@SUBNET@
MODE="${1:-run}"

ensure_rule() { # <priority> <regex of the action in `ip rule show`> <ip rule action...>
    local prio=$1 pattern=$2
    shift 2
    if ip rule show | grep -Eq "^${prio}:[[:space:]]+from ${SUBNET}[[:space:]]+${pattern}([[:space:]]|\$)"; then
        return 0
    fi
    ip rule add priority "$prio" from "$SUBNET" "$@"
}

ensure_ipt() { # <table> <chain> <rule...>
    local table=$1
    shift
    iptables -t "$table" -C "$@" 2>/dev/null || iptables -t "$table" -A "$@"
}

# Russian-direct table (filled by apply-ru.sh). While it is empty, the rule falls through to the prohibit rule below.
ensure_rule @RU_PRIORITY@ 'lookup (@RU_TABLE@|@RU_TABLE_SPARE@|ru)' lookup @RU_TABLE@
# Russian-direct routes (stage 24.4): after a container restart the panel's list is loaded again. Only on start: the
# `apply` mode (repair of a running container) must not switch the tables every time.
if [ "$MODE" != apply ] && [ -f "$GW_DIR/@RU_FILE@" ] && [ -f "$GW_DIR/@APPLY_RU_SCRIPT@" ]; then
    bash "$GW_DIR/@APPLY_RU_SCRIPT@" || echo "start.sh: Russian routes were not loaded" >&2
fi
# Kill-switch: client traffic never reaches the main table, so nothing leaks out of the Russian IP and the host and the
# docker network stay unreachable. Every route for clients lives in the tables of the rules above this one.
ensure_rule @KILL_PRIORITY@ prohibit prohibit
# Clients are isolated from each other.
ensure_ipt filter FORWARD -i "$GW_IF" -o "$GW_IF" -j DROP
# An exit (even a compromised one) cannot start connections into the clients' network: only replies get through.
ensure_ipt filter FORWARD -i "@EXIT_IF_GLOB@" -m conntrack ! --ctstate ESTABLISHED,RELATED -j DROP

# Exit assignment (stage 24.3): GW_ASSIGN marks client packets with the mark of their exit; the exit tunnel's ip rule
# @EXIT_RULE_PRIORITY@ sends marked packets to the exit table. CONNMARK keeps the mark for the whole connection, so
# existing connections stay on their exit when an assignment changes. No mark - no rule - the prohibit rule above.
iptables -t mangle -N @ASSIGN_CHAIN@ 2>/dev/null || true
ensure_ipt mangle PREROUTING -i "$GW_IF" -j CONNMARK --restore-mark
ensure_ipt mangle PREROUTING -i "$GW_IF" -m mark --mark 0 -j @ASSIGN_CHAIN@
ensure_ipt mangle PREROUTING -i "$GW_IF" -j CONNMARK --save-mark
# The panel's assignment map survives a container restart.
if [ -f "$GW_DIR/@ASSIGN_FILE@" ]; then
    iptables-restore -w --noflush < "$GW_DIR/@ASSIGN_FILE@" || echo "start.sh: assignments were not loaded" >&2
fi

if [ "$MODE" != apply ]; then
    awg-quick up "$GW_CONF" || { echo "start.sh: $GW_IF failed to come up" >&2; exit 1; }
fi

if [ -d "$GW_DIR/@EXITS_DIR@" ]; then
    for conf in "$GW_DIR/@EXITS_DIR@"/*.conf; do
        [ -e "$conf" ] || continue
        name=$(basename "$conf" .conf)
        if ip link show "$name" >/dev/null 2>&1; then
            continue
        fi
        awg-quick up "$conf" || echo "start.sh: exit tunnel $name failed to come up" >&2
    done
fi

ensure_ipt mangle FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu \
    || echo "start.sh: TCP MSS clamping is unavailable on this host" >&2

if [ "$MODE" = apply ]; then
    exit 0
fi

trap 'exit 0' TERM INT
tail -f /dev/null &
wait $!
"""


def render_start_script(layout: ContainerLayout) -> str:
    """Текст start.sh для раскладки. NAT для российского напрямую (MASQUERADE) здесь не ставится: его даёт PostUp
    серверного конфига (`nat_post_up` раскладки), и повторять его скриптом не нужно."""
    values = {
        "CONTAINER_DIR": layout.container_config_dir,
        "INTERFACE": layout.interface_name,
        "CONFIG_FILE": layout.config_file_name,
        "SUBNET": layout.tunnel_subnet,
        "RU_PRIORITY": str(RU_RULE_PRIORITY),
        "RU_TABLE": str(RU_TABLE),
        "KILL_PRIORITY": str(KILL_SWITCH_PRIORITY),
        "EXITS_DIR": EXITS_DIR_NAME,
        "ASSIGN_CHAIN": ASSIGN_CHAIN,
        "ASSIGN_FILE": ASSIGN_FILE_NAME,
        "EXIT_RULE_PRIORITY": str(EXIT_RULE_PRIORITY),
        "EXIT_IF_GLOB": f"{EXIT_INTERFACE_PREFIX}+",
        "RU_TABLE_SPARE": str(RU_TABLE_SPARE),
        "RU_FILE": RU_FILE_NAME,
        "APPLY_RU_SCRIPT": APPLY_RU_SCRIPT_NAME,
    }
    text = _START_SCRIPT
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    return text


def has_kill_switch(ip_rule_output: str, subnet: str) -> bool:
    """В выводе `ip rule show` есть `<приоритет>: from <подсеть клиентов> prohibit`."""
    pattern = re.compile(rf"^{KILL_SWITCH_PRIORITY}:\s+from {re.escape(subnet)}\s+prohibit(\s|$)")
    return any(pattern.match(line.strip()) for line in ip_rule_output.splitlines())


def exit_interfaces(awg_show_interfaces: str, gateway_interface: str) -> list[str]:
    """Интерфейсы выходов: все туннели контейнера, кроме клиентского `gw0` (`awg show interfaces`)."""
    return [name for name in awg_show_interfaces.split() if name != gateway_interface]


_PUBLISHED_RE = re.compile(r"(\d{1,5})(?:-(\d{1,5}))?->")


def parse_published_ports(docker_ps_ports: str) -> list[tuple[int, int]]:
    """Опубликованные на хосте порты из `docker ps --format '{{.Ports}}'` как диапазоны (от, до) включительно.

    Строка вида `0.0.0.0:34567->34567/udp, [::]:34567->34567/udp, 0.0.0.0:100-110->100-110/tcp`. TCP тоже
    считается занятым: лишняя осторожность не стоит ничего, а порт шлюза выбирается случайно. Вывод - с сервера,
    поэтому всё нераспознанное пропускается без ошибки.
    """
    ranges: list[tuple[int, int]] = []
    for part in docker_ps_ports.replace("\n", ",").split(","):
        host_side = part.strip().split("->", 1)
        if len(host_side) != 2:
            continue
        match = _PUBLISHED_RE.search(host_side[0].rsplit(":", 1)[-1] + "->")
        if match is None:
            continue
        low = int(match.group(1))
        high = int(match.group(2) or low)
        if low <= high <= 65535:
            ranges.append((low, high))
    return ranges


# --- выходы (этап 24.2) -----------------------------------------------------------------------------------------
#
# Шлюз - клиент своих выходов: на каждом выходе панель создаёт «внутреннего клиента» обычным create_client, а на шлюзе
# поднимает AWG-интерфейс gwx<слот> с конфигом этого клиента. Слот - целое 1..200, из него и только из него строятся имя
# интерфейса, имя файла и номер таблицы маршрутов: ничего пользовательского в командах и конфиге нет.

EXIT_INTERFACE_PREFIX = "gwx"
MAX_EXIT_SLOT = 200
# Таблица маршрутов выхода = EXIT_TABLE_BASE + слот. База 1000, а не 100: 100 + слот попадал бы на зарезервированные
# 253/254/255 (default/main/local - `ip route add ... table 254` изменил бы main хоста) и на RU_TABLE = 200 (слот 100).
EXIT_TABLE_BASE = 1000
EXIT_MTU = 1280  # туннель выхода вложен в туннель клиента и в физическую сеть; клиенты используют ещё меньше
EXIT_ALLOWED_IPS = "0.0.0.0/0"  # шлюз отправляет в выход всё, что ему назначено таблицей (IPv6 на шлюзе выключен)
EXIT_KEEPALIVE = CLIENT_PERSISTENT_KEEPALIVE  # рукопожатие сразу после up и дальше без трафика клиентов

_EXIT_INTERFACE_RE = re.compile(rf"{EXIT_INTERFACE_PREFIX}([1-9][0-9]{{0,2}})")
# Ключи [Interface] конфига, которые меняют сетевую часть: поменялись - интерфейс пересоздаётся, а не syncconf.
_EXIT_RESTART_KEYS = ("Address", "MTU", "Table", "PostUp", "PostDown")
# Значения обфускации (из конфига клиента, а он - из серверного конфига выхода): одна строка без служебных символов.
_OBFUSCATION_VALUE_RE = re.compile(r"[\w<>=,:+/ .\-]{1,512}")
_HOSTNAME_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"{_HOSTNAME_LABEL}(?:\.{_HOSTNAME_LABEL})*")
_TRANSFER_RE = re.compile(r"\S+\s+(\d{1,18})\s+(\d{1,18})")


def _slot(slot: int) -> int:
    """Слот - только целое 1..MAX_EXIT_SLOT (bool и строки не годятся): остальное в команды не попадает."""
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= MAX_EXIT_SLOT:
        raise ValueError(f"Exit slot must be an integer from 1 to {MAX_EXIT_SLOT}")
    return slot


def exit_interface(slot: int) -> str:
    return f"{EXIT_INTERFACE_PREFIX}{_slot(slot)}"


def exit_table(slot: int) -> int:
    return EXIT_TABLE_BASE + _slot(slot)


def exit_slot_from_interface(name: str) -> int | None:
    """Слот по имени интерфейса `gwx<N>` или None (другой интерфейс, в том числе `gw0`, и слот вне диапазона)."""
    match = _EXIT_INTERFACE_RE.fullmatch(name)
    if match is None:
        return None
    slot = int(match.group(1))
    return slot if slot <= MAX_EXIT_SLOT else None


def exit_host_dir(layout: ContainerLayout) -> str:
    return f"{layout.host_config_dir}/{EXITS_DIR_NAME}"


def exit_host_file(layout: ContainerLayout, slot: int) -> str:
    """Файл туннеля выхода на хосте (папка шлюза смонтирована в контейнер целиком)."""
    return f"{exit_host_dir(layout)}/{exit_interface(slot)}.conf"


def exit_container_file(layout: ContainerLayout, slot: int) -> str:
    return f"{layout.container_config_dir}/{EXITS_DIR_NAME}/{exit_interface(slot)}.conf"


def exit_nat_rule(slot: int) -> str:
    return f"POSTROUTING -o {exit_interface(slot)} -j MASQUERADE"


def exit_post_up(slot: int) -> str:
    """Подмена адреса клиентов на адрес внутреннего клиента на выходе. Идемпотентно: повторный up не плодит правил."""
    rule = exit_nat_rule(slot)
    return f"iptables -t nat -C {rule} 2>/dev/null || iptables -t nat -A {rule}"


def exit_post_down(slot: int) -> str:
    """Снятие правила; `|| true`: уже снятое правило не должно срывать `awg-quick down` (в нём `set -e`)."""
    return f"iptables -t nat -D {exit_nat_rule(slot)} 2>/dev/null || true"


def exit_mark(slot: int) -> int:
    """Метка fwmark клиентов выхода = номер его таблицы маршрутов (1000 + слот): метка и таблица совпадают."""
    return exit_table(slot)


def exit_rule_up(slot: int) -> str:
    """Правило «пакеты с меткой выхода -> таблица выхода». Живёт и умирает вместе с интерфейсом (PostUp/PostDown).
    Повторное добавление - EEXIST, поэтому `|| true`."""
    mark = exit_mark(slot)
    return f"ip rule add priority {EXIT_RULE_PRIORITY} fwmark {mark} lookup {mark} 2>/dev/null || true"


def exit_rule_down(slot: int) -> str:
    mark = exit_mark(slot)
    return f"ip rule del priority {EXIT_RULE_PRIORITY} fwmark {mark} lookup {mark} 2>/dev/null || true"


def format_endpoint(host: str, port: int) -> str:
    """`host:port` для Endpoint; адрес IPv6 - в скобках. Проверяется в render_exit_config."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _validated_endpoint(endpoint: str) -> str:
    """Endpoint идёт прямо в конфиг, а host сервера вводит админ: допускаются только имя хоста / IPv4 / IPv6 в скобках
    и порт 1..65535 - никаких пробелов, переводов строки и `#` (иначе в конфиг попала бы лишняя строка)."""
    host, separator, port = endpoint.rpartition(":")
    if not separator or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid exit endpoint")
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise ValueError("Invalid exit endpoint") from None
    elif not _HOST_RE.fullmatch(host) or len(host) > 253:
        raise ValueError("Invalid exit endpoint")
    return f"{host}:{int(port)}"


@dataclass(frozen=True)
class ExitTunnel:
    """Туннель шлюза к выходу: слот, конфиг внутреннего клиента на выходе (результат create_client; секрет - не в repr)
    и адрес выхода `host:port`."""

    slot: int
    client_config: str = field(repr=False)
    endpoint: str


@dataclass(frozen=True)
class RuStatus:
    """«Россия напрямую» на шлюзе: активная таблица (None - правила 100 нет), число маршрутов в ней и отпечаток списка,
    который скрипт применил последним (`ru.applied`; None - файла нет)."""

    table: int | None
    routes: int
    digest: str | None = None


@dataclass(frozen=True)
class ExitTunnelStatus:
    """Состояние туннеля выхода на шлюзе. Ключей нет: слот, счётчики и время рукопожатия можно показывать."""

    slot: int
    interface_up: bool
    latest_handshake: datetime | None
    rx_bytes: int = 0
    tx_bytes: int = 0


def render_exit_config(tunnel: ExitTunnel) -> str:
    """Конфиг интерфейса gwx<слот> для `awg-quick` на шлюзе - из конфига внутреннего клиента на выходе.

    Из [Interface] берутся PrivateKey, Address (/32 клиента) и обфускация по белому списку (тот же, что при выдаче
    конфига клиенту); DNS, PostUp, Table и прочее из чужого конфига не попадают. `Table = EXIT_TABLE_BASE + слот`:
    awg-quick кладёт маршрут по умолчанию в эту таблицу и не трогает main хоста. Ошибка формата - ValueError без
    содержимого конфига (в нём ключи).
    """
    slot = _slot(tunnel.slot)
    endpoint = _validated_endpoint(tunnel.endpoint)
    interface, peer = client_config_sections(tunnel.client_config)
    try:
        private_key, public_key, psk = interface["PrivateKey"], peer["PublicKey"], peer["PresharedKey"]
        address = ipaddress.ip_interface(interface["Address"].split(",")[0].strip()).ip
    except (KeyError, ValueError):
        raise ValueError("Client config is malformed") from None
    if not (is_valid_public_key(private_key) and is_valid_public_key(public_key) and is_valid_public_key(psk)):
        raise ValueError("Client config is malformed")
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError("Client config address must be IPv4")
    obfuscation = obfuscation_params(interface)
    if not all(_OBFUSCATION_VALUE_RE.fullmatch(value) for value in obfuscation.values()):
        raise ValueError("Client config is malformed")
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {address}/32",
        *(f"{key} = {value}" for key, value in obfuscation.items()),
        f"MTU = {EXIT_MTU}",
        f"Table = {exit_table(slot)}",
        f"PostUp = {exit_post_up(slot)}",
        f"PostUp = {exit_rule_up(slot)}",
        f"PostDown = {exit_rule_down(slot)}",
        f"PostDown = {exit_post_down(slot)}",
        "",
        "[Peer]",
        f"PublicKey = {public_key}",
        f"PresharedKey = {psk}",
        f"Endpoint = {endpoint}",
        f"AllowedIPs = {EXIT_ALLOWED_IPS}",
        f"PersistentKeepalive = {EXIT_KEEPALIVE}",
    ]
    return "\n".join(lines) + "\n"


def exit_needs_restart(old_config: str | None, new_config: str) -> bool:
    """Нужно ли пересоздавать интерфейс: поменялись Address / MTU / Table / хуки (syncconf их не применяет). Ключи,
    обфускация и Endpoint меняются на живом интерфейсе. Старого конфига нет или он не разбирается - пересоздаём."""
    if old_config is None:
        return True
    try:
        interface_values(old_config)
        interface_values(new_config)
    except ValueError:
        return True
    return _restart_entries(old_config) != _restart_entries(new_config)


def _restart_entries(config: str) -> dict[str, list[str]]:
    """Значения ключей _EXIT_RESTART_KEYS из [Interface] списками (PostUp/PostDown бывают по нескольку строк)."""
    entries: dict[str, list[str]] = {key: [] for key in _EXIT_RESTART_KEYS}
    in_interface = False
    for raw in config.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_interface = line == "[Interface]"
        elif in_interface and "=" in line and not line.startswith("#"):
            key, value = (part.strip() for part in line.split("=", 1))
            if key in entries:
                entries[key].append(value)
    return entries


def exit_up_command(layout: ContainerLayout, slot: int) -> str:
    return f"docker exec {layout.container_name} awg-quick up {exit_container_file(layout, slot)}"


def exit_down_command(layout: ContainerLayout, slot: int) -> str:
    return f"docker exec {layout.container_name} awg-quick down {exit_container_file(layout, slot)}"


def exit_delete_link_command(layout: ContainerLayout, slot: int) -> str:
    """Запасной путь, когда файла туннеля нет, а интерфейс остался: `awg-quick down` без файла не работает."""
    return f"docker exec {layout.container_name} ip link delete dev {exit_interface(slot)}"


def exit_nat_cleanup_command(layout: ContainerLayout, slot: int) -> str:
    """Снять правило NAT туннеля вручную: `ip link delete` не выполняет PostDown, и правило висело бы без интерфейса.
    `|| true` - на стороне хоста: отсутствие правила не ошибка."""
    return f"docker exec {layout.container_name} iptables -t nat -D {exit_nat_rule(slot)} || true"


def exit_show_command(layout: ContainerLayout, slot: int, what: str) -> str:
    """`awg show gwx<N> latest-handshakes|transfer` (what - одна из двух констант)."""
    if what not in ("latest-handshakes", "transfer"):
        raise ValueError("Unsupported awg show field")
    return f"docker exec {layout.container_name} awg show {exit_interface(slot)} {what}"


def exit_sync_command(layout: ContainerLayout, slot: int) -> str:
    """Применить изменённый конфиг выхода к живому интерфейсу без пересоздания. Безопасный вариант (23.3): strip во
    временный файл `&&` syncconf. Никогда `syncconf <(strip)`: при упавшем strip он получает пустой конфиг и сносит
    всех peer'ов. Файл с приватным ключом удаляется при любом исходе."""
    interface = exit_interface(slot)
    stripped = f"/tmp/{interface}.stripped"
    # umask 077: во временном файле приватный ключ, а umask контейнера дал бы 0644.
    script = (
        f"umask 077; awg-quick strip {exit_container_file(layout, slot)} > {stripped}"
        f" && awg syncconf {interface} {stripped}; rc=$?; rm -f {stripped}; exit $rc"
    )
    return f"docker exec {layout.container_name} sh -c '{script}'"


def parse_transfer(output: str) -> tuple[int, int]:
    """Сумма (принято, отправлено) байт по peer'ам из `awg show <iface> transfer`; кривые строки пропускаются."""
    rx = tx = 0
    for line in output.splitlines():
        match = _TRANSFER_RE.fullmatch(line.strip())
        if match:
            rx, tx = rx + int(match.group(1)), tx + int(match.group(2))
    return rx, tx


def exit_health_label(status: ExitTunnelStatus | None, now: datetime) -> str:
    """ok / stale (рукопожатие старше окна) / no_handshake (ещё не было) / down (интерфейса нет)."""
    if status is None or not status.interface_up:
        return "down"
    if status.latest_handshake is None:
        return "no_handshake"
    age = (now - status.latest_handshake).total_seconds()
    return "ok" if age <= ACTIVE_HANDSHAKE_WINDOW_SECONDS else "stale"


# --- назначение выходов клиентам (этап 24.3) --------------------------------------------------------------------
#
# Панель держит на шлюзе файл assign.rules (формат iptables-restore, секция *mangle): по правилу на клиента, которое
# ставит метку его выхода. Файл применяется атомарно (`--test`, затем `--noflush`; объявление цепочки `:GW_ASSIGN`
# очищает её, поэтому каждое применение - полная замена карты) и читается start.sh при запуске контейнера.

_ASSIGN_RULE_RE = re.compile(
    rf"-A {ASSIGN_CHAIN} -s (\d{{1,3}}(?:\.\d{{1,3}}){{3}})(?:/32)?"
    r" -j MARK --set-(?:x?mark) (0x[0-9a-fA-F]+|\d+)(?:/0x[0-9a-fA-F]+)?"
)


def render_assign_rules(assignments: Mapping[ipaddress.IPv4Address, int], subnet: str = GATEWAY_SUBNET) -> str:
    """Текст assign.rules по полной карте «адрес клиента -> слот выхода» (пустая допустима). В строки попадают только
    проверенные IPv4Address из подсети клиентов и метки целых слотов; порядок - по адресу, текст стабилен."""
    network = ipaddress.ip_network(subnet)
    lines = ["*mangle", f":{ASSIGN_CHAIN} - [0:0]"]
    for address, slot in sorted(assignments.items()):
        if not isinstance(address, ipaddress.IPv4Address) or address not in network:
            raise ValueError("Assignment address must be an IPv4 address inside the client subnet")
        lines.append(f"-A {ASSIGN_CHAIN} -s {address}/32 -j MARK --set-mark {exit_mark(slot)}")
    return "\n".join([*lines, "COMMIT"]) + "\n"


def assign_apply_command(layout: ContainerLayout) -> str:
    """Применить assign.rules атомарно: сначала `--test` (ничего не меняет), затем настоящее применение."""
    path = f"{layout.container_config_dir}/{ASSIGN_FILE_NAME}"
    # -w: ждать xtables-lock (параллельный iptables не должен ронять применение карты).
    script = f"iptables-restore -w --test --noflush < {path} && iptables-restore -w --noflush < {path}"
    return f"docker exec {layout.container_name} sh -c '{script}'"


def assign_show_command(layout: ContainerLayout) -> str:
    return f"docker exec {layout.container_name} iptables -t mangle -S {ASSIGN_CHAIN}"


PREROUTING_SHOW = "iptables -t mangle -S PREROUTING"


def prerouting_show_command(layout: ContainerLayout) -> str:
    return f"docker exec {layout.container_name} {PREROUTING_SHOW}"


def has_assign_hooks(prerouting_output: str, interface: str) -> bool:
    """В `iptables -t mangle -S PREROUTING` есть все три прыжка start.sh и именно в таком порядке: восстановить метку
    соединения -> назначить выход (GW_ASSIGN, только для пакетов без метки) -> сохранить метку в соединении. Сама
    цепочка GW_ASSIGN прыжков не доказывает: `iptables-restore` создаёт её сам, даже если start.sh старый."""
    prefix = f"-A PREROUTING -i {interface} "
    found: list[str] = []
    for raw in prerouting_output.splitlines():
        line = raw.strip()
        if not line.startswith(prefix):
            continue
        rest = line.removeprefix(prefix)
        if rest.startswith("-j CONNMARK --restore-mark"):
            found.append("restore")
        elif re.fullmatch(rf"-m mark --mark (?:0x0|0) -j {ASSIGN_CHAIN}", rest):
            found.append("assign")
        elif rest.startswith("-j CONNMARK --save-mark"):
            found.append("save")
    return found == ["restore", "assign", "save"]


def missing_exit_rules(ip_rule_output: str, slots: Iterable[int]) -> list[int]:
    """Слоты поднятых выходов, у которых нет `ip rule` метки (`1000: from all fwmark 0x.. lookup ..`)."""
    present = {line.strip() for line in ip_rule_output.splitlines()}
    missing = []
    for slot in slots:
        mark = exit_mark(slot)
        pattern = re.compile(rf"{EXIT_RULE_PRIORITY}:\s+from all fwmark 0x{mark:x} lookup {mark}")
        if not any(pattern.fullmatch(" ".join(line.split())) for line in present):
            missing.append(slot)
    return missing


def assign_host_file(layout: ContainerLayout) -> str:
    return f"{layout.host_config_dir}/{ASSIGN_FILE_NAME}"


def parse_assign_chain(output: str) -> dict[ipaddress.IPv4Address, int]:
    """Фактическая карта из `iptables -t mangle -S GW_ASSIGN`: адрес клиента -> слот. Вывод - с сервера (недоверенный):
    кривые строки и метки вне диапазона слотов пропускаются."""
    found: dict[ipaddress.IPv4Address, int] = {}
    for line in output.splitlines():
        match = _ASSIGN_RULE_RE.fullmatch(line.strip())
        if match is None:
            continue
        try:
            address = ipaddress.IPv4Address(match.group(1))
            slot = int(match.group(2), 0) - EXIT_TABLE_BASE
        except ValueError:
            continue
        if 1 <= slot <= MAX_EXIT_SLOT:
            found[address] = slot
    return found


# --- Россия напрямую (этап 24.4) ---------------------------------------------------------------------------------
#
# Клиент идёт к российскому адресу напрямую с адреса шлюза: правило `ip rule 100 from <подсеть клиентов> lookup <ru>`
# (приоритет выше меток выходов 1000 и kill-switch 9000) отправляет его в таблицу `ru` через шлюз контейнера по
# умолчанию (eth0), MASQUERADE `-s <подсеть> -o eth0` из gw0.conf даёт адрес шлюза. Таблицы две (200 и 201): новый
# список собирается в свободной, затем правило 100 переключается на неё, и старая очищается - окна без правила у
# работающих клиентов нет.

_APPLY_RU_SCRIPT = r"""#!/bin/bash
# Generated by the VPN panel and rewritten on every apply - do not edit.
# Replaces the Russian-direct routes atomically: fills the spare table (@TABLE_A@ <-> @TABLE_B@), then moves ip rule
# @PRIORITY@ to it and clears the old one. On any failure the spare table is cleared and the active one is untouched.
set -eu

LIST=@CONTAINER_DIR@/@RU_FILE@
SUBNET=@SUBNET@
PRIO=@PRIORITY@
TABLE_A=@TABLE_A@
TABLE_B=@TABLE_B@
APPLIED=@CONTAINER_DIR@/@APPLIED_FILE@

fail() { echo "apply-ru.sh: $*" >&2; exit 1; }

# One run at a time (the panel and start.sh can overlap): the lock is held until this shell exits.
exec 9> /run/apply-ru.lock
flock -x 9

[ -f "$LIST" ] || fail "$LIST is missing"
# The first line of the list is "# sha256=<hex>" (the digest of the prefixes below); it is recorded after a success.
digest=$(sed -n '1s/^# sha256=\([0-9a-f]\{64\}\)$/\1/p' "$LIST")

gateway="" dev=""
if [[ $(ip route show default | head -n1) =~ ^default\ via\ ([0-9.]+)\ dev\ ([A-Za-z0-9_.-]+) ]]; then
    gateway=${BASH_REMATCH[1]}
    dev=${BASH_REMATCH[2]}
fi
[ -n "$gateway" ] && [ -n "$dev" ] || fail "no default gateway in the container"

# The active table is the one ip rule PRIO points to.
rule_to() { [[ $1 =~ ^${PRIO}:[[:space:]]+from[[:space:]]+${SUBNET}[[:space:]]+lookup[[:space:]]+$2([[:space:]]|$) ]]; }
has_a=0 has_b=0
while read -r line; do
    rule_to "$line" "$TABLE_A" && has_a=1
    rule_to "$line" "$TABLE_B" && has_b=1
done <<< "$(ip rule show)"
active="" spare=$TABLE_A
if [ "$has_a" = 1 ] && [ "$has_b" = 1 ]; then # leftover of an interrupted run: keep A, drop the rule to B
    ip rule del priority "$PRIO" from "$SUBNET" lookup "$TABLE_B"
    ip route flush table "$TABLE_B" 2>/dev/null || true
    has_b=0
fi
if [ "$has_a" = 1 ]; then active=$TABLE_A; spare=$TABLE_B; fi
if [ "$has_b" = 1 ]; then active=$TABLE_B; spare=$TABLE_A; fi

batch=$(mktemp)
trap 'rm -f "$batch"' EXIT
ip route flush table "$spare" 2>/dev/null || true
count=0
while read -r cidr; do
    [ -n "$cidr" ] || continue
    [[ $cidr == "#"* ]] && continue
    if ! [[ $cidr =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]]; then
        ip route flush table "$spare" 2>/dev/null || true
        fail "the list contains a line that is not a prefix"
    fi
    echo "route replace $cidr via $gateway dev $dev table $spare"
    count=$((count + 1))
done < "$LIST" > "$batch"
[ "$count" -gt 0 ] || fail "the list is empty"

if ! ip -batch "$batch"; then
    ip route flush table "$spare" 2>/dev/null || true
    fail "ip -batch failed, the active table is untouched"
fi

# Switch: the new rule first, then the old one goes away - there is no moment without a rule for working clients.
ip rule add priority "$PRIO" from "$SUBNET" lookup "$spare"
if [ -n "$active" ]; then
    ip rule del priority "$PRIO" from "$SUBNET" lookup "$active"
    # An empty table does not exist for the kernel: "FIB table does not exist" is not a failure here.
    ip route flush table "$active" 2>/dev/null || true
fi
# What was applied, for the panel to compare with what it expects (a failed write is repaired by the next sync).
if [ -n "$digest" ]; then
    { printf '%s\n' "$digest" > "$APPLIED.tmp" && mv -f "$APPLIED.tmp" "$APPLIED"; } || echo "apply-ru.sh: no mark" >&2
else
    rm -f "$APPLIED"
fi
echo "apply-ru.sh: $count routes in table $spare"
"""


def render_apply_ru_script(layout: ContainerLayout) -> str:
    """Текст apply-ru.sh для раскладки. Подстановки - только константы раскладки; в аргументах скрипта и в его командах
    пользовательского ввода нет (список читается из файла, строки проверяются регулярным выражением)."""
    values = {
        "TABLE_A": str(RU_TABLE),
        "TABLE_B": str(RU_TABLE_SPARE),
        "PRIORITY": str(RU_RULE_PRIORITY),
        "CONTAINER_DIR": layout.container_config_dir,
        "RU_FILE": RU_FILE_NAME,
        "APPLIED_FILE": RU_APPLIED_NAME,
        "SUBNET": layout.tunnel_subnet,
    }
    text = _APPLY_RU_SCRIPT
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    return text


def ru_prefixes_file(layout: ContainerLayout) -> str:
    return f"{layout.host_config_dir}/{RU_FILE_NAME}"


def ru_applied_file(layout: ContainerLayout) -> str:
    return f"{layout.host_config_dir}/{RU_APPLIED_NAME}"


def apply_ru_script_file(layout: ContainerLayout) -> str:
    return f"{layout.host_config_dir}/{APPLY_RU_SCRIPT_NAME}"


def apply_ru_command(layout: ContainerLayout) -> str:
    return f"docker exec {layout.container_name} bash {layout.container_config_dir}/{APPLY_RU_SCRIPT_NAME}"


def ru_routes_command(layout: ContainerLayout, table: int) -> str:
    """Маршруты таблицы `ru` (только чтение); таблица - одна из двух констант."""
    if table not in RU_TABLES:
        raise ValueError("Not a Russian-direct table")
    return f"docker exec {layout.container_name} ip route show table {table}"


def _ru_network_ok(network: object) -> bool:
    """Сеть можно положить в ru.prefixes: IPv4, не крупнее /8, вне частных / служебных диапазонов (общий источник правды
    со списком - `split_routes.py`; шлюз проверяет сам, не полагаясь на панель)."""
    return is_routable(network, MIN_ROUTE_PREFIXLEN)


def render_ru_prefixes(prefixes: Iterable[ipaddress.IPv4Network]) -> str:
    """Префиксы для ru.prefixes: CIDR по строке, по возрастанию. Только проверенные IPv4Network вне зарезервированных
    диапазонов и не крупнее /8: по этому файлу `ip -batch` на шлюзе строит маршруты."""
    networks = sorted(set(prefixes))
    for network in networks:
        if not _ru_network_ok(network):
            raise ValueError("A Russian-direct prefix must be a public IPv4 network outside the reserved ranges")
    return "".join(f"{network}\n" for network in networks)


def ru_digest(body: str) -> str:
    """Отпечаток списка префиксов (текста `render_ru_prefixes`)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def render_ru_file(prefixes: Iterable[ipaddress.IPv4Network]) -> tuple[str, str]:
    """Файл ru.prefixes и отпечаток: первая строка `# sha256=<hex>`, дальше префиксы. Пустой список - ("", "")."""
    body = render_ru_prefixes(prefixes)
    if not body:
        return "", ""
    digest = ru_digest(body)
    return f"# sha256={digest}\n{body}", digest


def ru_active_table(ip_rule_output: str, subnet: str) -> int | None:
    """Таблица, на которую указывает `ip rule` приоритета 100 для подсети клиентов (200 или 201), либо None."""
    pattern = re.compile(rf"{RU_RULE_PRIORITY}:\s+from {re.escape(subnet)}\s+lookup (\d+)(?:\s|$)")
    for line in ip_rule_output.splitlines():
        match = pattern.match(line.strip())
        if match and int(match.group(1)) in RU_TABLES:
            return int(match.group(1))
    return None
