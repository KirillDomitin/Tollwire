#!/usr/bin/python
# -*- coding: utf-8 -*-
import base64
import ipaddress
import random
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from core.exceptions import AddressPoolExhaustedError
from infrastructure.vpn.config import (
    CLIENT_ALLOWED_IPS,
    CLIENT_DNS,
    CLIENT_PERSISTENT_KEEPALIVE,
    DEFAULT_AWG_CONFIG,
    NAT_POST_DOWN,
    NAT_POST_UP,
)

# Ключи серверного [Interface], которые КОПИРУЮТСЯ в конфиг клиента: параметры обфускации AmneziaWG — они обязаны
# совпадать у сервера и клиента. Список разрешённых (allowlist), а не запрещённых: неизвестный ключ из серверного
# конфига (ручная правка, скомпрометированный сервер) к клиенту не попадёт. Иначе PreUp/PostUp/Table/SaveConfig
# из [Interface] сервера выполнились бы на устройстве клиента — wg-quick запускает их от root.
_CLIENT_INTERFACE_KEYS = frozenset(
    {
        *("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"),
        *("I1", "I2", "I3", "I4", "I5"),
        *("ContentPaddingAddition", "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime"),
        *("KeepaliveTimeout", "MaxHandshakeAttempts", "RandomTrailers", "DisableCookies"),
    }
)
_PUBLIC_KEY_RE = re.compile(r"[A-Za-z0-9+/]{43}=")
# Комментарий с именем клиента в [Peer]: WireGuard его игнорирует, а мы по нему узнаём своих peer'ов.
_PEER_NAME_PREFIX = "# client:"


@dataclass
class AWGKeys:
    private_key: str
    public_key: str
    psk: str


def generate_secret_keys() -> AWGKeys:
    private_key = x25519.X25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    private_key_str = base64.b64encode(private_bytes).decode()
    public_key_str = base64.b64encode(public_bytes).decode()

    psk_bytes = secrets.token_bytes(32)  # 32 случайных байта
    psk_str = base64.b64encode(psk_bytes).decode()

    return AWGKeys(private_key=private_key_str, public_key=public_key_str, psk=psk_str)


def pick_udp_port() -> int:
    return random.randint(20000, 60000)


@dataclass(frozen=True)
class Obfuscation:
    """Параметры обфускации AmneziaWG — общие для сервера и всех его клиентов, свои у каждого сервера.

    jc/jmin/jmax — сколько мусорных пакетов и какого размера слать перед рукопожатием;
    s1/s2 — случайная добавка к размеру пакетов рукопожатия (init и response);
    h1..h4 — «магические» заголовки четырёх типов пакетов вместо стандартных 1..4 WireGuard.
    """

    jc: int
    jmin: int
    jmax: int
    s1: int
    s2: int
    h1: int
    h2: int
    h3: int
    h4: int

    def lines(self) -> str:
        values = {"Jc": self.jc, "Jmin": self.jmin, "Jmax": self.jmax, "S1": self.s1, "S2": self.s2}
        values |= {"H1": self.h1, "H2": self.h2, "H3": self.h3, "H4": self.h4}
        return "\n".join(f"{key} = {value}" for key, value in values.items())


_INIT_SIZE, _RESPONSE_SIZE = 148, 92  # размеры init/response пакетов WireGuard без добавок S1/S2


def random_obfuscation() -> Obfuscation:
    """Случайные допустимые параметры (криптостойкий генератор — значения не предсказать по соседнему серверу).

    Ограничения AmneziaWG:
    - init (148+S1) и response (92+S2) не должны совпасть по размеру, иначе их не различить: S2 != S1 + 56;
    - H1..H4 — разные и не 1..4 (это заголовки обычного WireGuard, ради отличия от них всё и затевается);
    - Jmin < Jmax <= 1280.
    """
    rng = secrets.SystemRandom()
    s1 = rng.randint(15, 150)
    s2 = rng.choice([s for s in range(15, 151) if _INIT_SIZE + s1 != _RESPONSE_SIZE + s])
    headers = rng.sample(range(5, 2**31), 4)
    jmin = rng.randint(10, 50)
    return Obfuscation(
        jc=rng.randint(4, 10),
        jmin=jmin,
        jmax=rng.randint(jmin + 20, jmin + 200),
        s1=s1,
        s2=s2,
        h1=headers[0],
        h2=headers[1],
        h3=headers[2],
        h4=headers[3],
    )


def render_awg_config(private_key_str: str, udp_port: int, obfuscation: Obfuscation | None = None) -> str:
    """Серверный конфиг новой установки. Без obfuscation — случайные параметры (для каждого сервера свои)."""
    chosen = obfuscation or random_obfuscation()
    return DEFAULT_AWG_CONFIG.format(private_key=private_key_str, port=udp_port, obfuscation=chosen.lines())


def is_valid_public_key(value: str) -> bool:
    """Ключ WireGuard — 32 байта в base64: ровно 43 символа алфавита и '=' в конце.

    fullmatch, а не match: у re.match «$» совпадает и перед завершающим \\n, и «ключ\\n» прошёл бы проверку.
    """
    return bool(_PUBLIC_KEY_RE.fullmatch(value))


def _parse_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    return key.strip(), value.strip()


def _split_sections(config: str) -> list[list[str]]:
    """Режет конфиг на секции. Первая строка секции — заголовок ([Interface]/[Peer]).

    Строки до первого заголовка (если есть) идут отдельной секцией без заголовка.
    Пустые строки остаются в секции, после которой стоят, — текст собирается обратно без потерь.
    """
    sections: list[list[str]] = [[]]
    for line in config.splitlines():
        if line.strip().startswith("["):
            sections.append([line])
        else:
            sections[-1].append(line)
    return [section for section in sections if section]


def _section_name(section: list[str]) -> str:
    return section[0].strip()


def _section_values(section: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in section[1:]:
        parsed = _parse_line(line)
        if parsed:
            values[parsed[0]] = parsed[1]
    return values


def _join_sections(sections: list[list[str]]) -> str:
    return "\n".join(line for section in sections for line in section).rstrip("\n") + "\n"


def ensure_nat_hooks(server_config: str) -> tuple[str, bool]:
    """Дописывает в серверный [Interface] хуки NAT, если их там нет (конфиги, созданные до фикса NAT).

    Возвращает новый текст и флаг «что-то изменилось». Чужие PostUp не трогаются — wg-quick
    выполняет все PostUp по порядку.
    """
    sections = _split_sections(server_config)
    for section in sections:
        if _section_name(section) != "[Interface]":
            continue
        post_ups = [p[1] for line in section[1:] if (p := _parse_line(line)) and p[0] == "PostUp"]
        if NAT_POST_UP in post_ups:
            return server_config, False
        # Вставляем после последней содержательной строки секции, чтобы пустые строки-разделители остались ниже.
        insert_at = max(i for i, line in enumerate(section) if line.strip()) + 1
        section[insert_at:insert_at] = [f"PostUp = {NAT_POST_UP}", f"PostDown = {NAT_POST_DOWN}"]
        return _join_sections(sections), True
    raise ValueError("Server config has no [Interface] section")


def interface_values(server_config: str) -> dict[str, str]:
    for section in _split_sections(server_config):
        if _section_name(section) == "[Interface]":
            return _section_values(section)
    raise ValueError("Server config has no [Interface] section")


def used_peer_addresses(server_config: str) -> set[ipaddress.IPv4Address]:
    used: set[ipaddress.IPv4Address] = set()
    for section in _split_sections(server_config):
        if _section_name(section) != "[Peer]":
            continue
        for item in _section_values(section).get("AllowedIPs", "").split(","):
            item = item.strip()
            if not item:
                continue
            address = ipaddress.ip_interface(item)
            if isinstance(address, ipaddress.IPv4Interface):
                used.add(address.ip)
    return used


def pick_free_address(server_config: str) -> ipaddress.IPv4Address:
    """Первый свободный адрес в подсети туннеля сервера.

    Подсеть берётся из Address серверного [Interface] (у нас 10.8.1.0/24, сервер сам занимает .0),
    занятые — из AllowedIPs всех [Peer]. Вызывающий обязан держать блокировку сервера, иначе два
    одновременных создания клиента получат один и тот же адрес.
    """
    server_address = ipaddress.ip_interface(interface_values(server_config)["Address"].split(",")[0].strip())
    taken = used_peer_addresses(server_config) | {server_address.ip}
    for host in server_address.network.hosts():
        if host not in taken and isinstance(host, ipaddress.IPv4Address):
            return host
    raise AddressPoolExhaustedError(f"No free addresses left in {server_address.network}")


def render_peer_block(name: str, public_key: str, psk: str, address: ipaddress.IPv4Address) -> str:
    return (
        f"[Peer]\n{_PEER_NAME_PREFIX} {name}\n"
        f"PublicKey = {public_key}\nPresharedKey = {psk}\nAllowedIPs = {address}/32\n"
    )


def append_peer(server_config: str, peer_block: str) -> str:
    return server_config.rstrip("\n") + "\n\n" + peer_block.rstrip("\n") + "\n"


def remove_peer(server_config: str, public_key: str) -> tuple[str, bool]:
    """Убирает [Peer] с данным PublicKey. Возвращает новый текст и флаг «что-то удалено»."""
    kept: list[list[str]] = []
    removed = False
    for section in _split_sections(server_config):
        if _section_name(section) == "[Peer]" and _section_values(section).get("PublicKey") == public_key:
            removed = True
            continue
        kept.append(section)
    return _join_sections(kept), removed


@dataclass
class PeerEntry:
    name: str | None
    public_key: str
    address: str | None


@dataclass
class ClientPeerParams:
    """Что нужно серверу, чтобы снова пустить клиента: выводится из конфига клиента."""

    public_key: str
    psk: str
    address: ipaddress.IPv4Address


def public_key_from_private(private_key: str) -> str:
    raw = base64.b64decode(private_key)
    public = x25519.X25519PrivateKey.from_private_bytes(raw).public_key()
    return base64.b64encode(public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _peer_name(section: list[str]) -> str | None:
    for line in section[1:]:
        stripped = line.strip()
        if stripped.startswith(_PEER_NAME_PREFIX):
            return stripped.removeprefix(_PEER_NAME_PREFIX).strip() or None
    return None


def list_peers(server_config: str) -> list[PeerEntry]:
    peers = []
    for section in _split_sections(server_config):
        if _section_name(section) != "[Peer]":
            continue
        values = _section_values(section)
        public_key = values.get("PublicKey")
        if not public_key:
            continue
        address = values.get("AllowedIPs", "").split(",")[0].strip().split("/")[0] or None
        peers.append(PeerEntry(name=_peer_name(section), public_key=public_key, address=address))
    return peers


def client_config_sections(client_config: str) -> tuple[dict[str, str], dict[str, str]]:
    """[Interface] и [Peer] конфига клиента как словари «ключ → значение»."""
    interface: dict[str, str] = {}
    peer: dict[str, str] = {}
    for section in _split_sections(client_config):
        if _section_name(section) == "[Interface]":
            interface = _section_values(section)
        elif _section_name(section) == "[Peer]":
            peer = _section_values(section)
    return interface, peer


def obfuscation_params(interface: dict[str, str]) -> dict[str, str]:
    """Параметры обфускации AmneziaWG из [Interface] — по тому же белому списку, что копируется сервер → клиент."""
    return {key: value for key, value in interface.items() if key in _CLIENT_INTERFACE_KEYS}


def parse_client_config(client_config: str) -> ClientPeerParams:
    """Достаёт из конфига клиента то, что нужно для его [Peer] на сервере. Приватный ключ наружу не отдаём."""
    interface, peer = client_config_sections(client_config)
    try:
        address = ipaddress.ip_interface(interface["Address"].split(",")[0].strip()).ip
        public_key = public_key_from_private(interface["PrivateKey"])
        psk = peer["PresharedKey"]
    except (KeyError, ValueError):
        # Без цепочки from exc: в тексте исходного исключения могут оказаться куски конфига.
        raise ValueError("Client config is malformed") from None
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError("Client config address must be IPv4")
    return ClientPeerParams(public_key=public_key, psk=psk, address=address)


def parse_latest_handshakes(output: str) -> dict[str, datetime | None]:
    """Разбирает `awg show <iface> latest-handshakes`: строки «ключ<TAB>unix-время», 0 — рукопожатий не было."""
    handshakes: dict[str, datetime | None] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        timestamp = int(parts[1])
        handshakes[parts[0]] = datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else None
    return handshakes


def render_client_config(
    *,
    server_config: str,
    client_private_key: str,
    client_address: ipaddress.IPv4Address,
    psk: str,
    server_public_key: str,
    endpoint_host: str,
) -> str:
    """Конфиг для приложения клиента. Параметры обфускации копируются из серверного [Interface]."""
    server_interface = interface_values(server_config)
    obfuscation = [f"{key} = {value}" for key, value in server_interface.items() if key in _CLIENT_INTERFACE_KEYS]
    host = f"[{endpoint_host}]" if ":" in endpoint_host else endpoint_host
    lines = [
        "[Interface]",
        f"PrivateKey = {client_private_key}",
        f"Address = {client_address}/32",
        f"DNS = {CLIENT_DNS}",
        *obfuscation,
        "",
        "[Peer]",
        f"PublicKey = {server_public_key}",
        f"PresharedKey = {psk}",
        f"Endpoint = {host}:{server_interface['ListenPort']}",
        f"AllowedIPs = {CLIENT_ALLOWED_IPS}",
        f"PersistentKeepalive = {CLIENT_PERSISTENT_KEEPALIVE}",
    ]
    return "\n".join(lines) + "\n"
