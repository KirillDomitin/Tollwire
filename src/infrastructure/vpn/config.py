#!/usr/bin/python
# -*- coding: utf-8 -*-
from dataclasses import dataclass

CONTAINER_NAME = "amnezia-awg"
# Политика перезапуска контейнера: без неё после перезагрузки сервера VPN остаётся выключенным.
RESTART_POLICY = "unless-stopped"
RESTARTING_POLICIES = frozenset({"unless-stopped", "always"})  # обе поднимают контейнер после перезагрузки
# Образ закреплён по дайджесту, а не :latest: контейнер работает на VPN-сервере с NET_ADMIN, и подменённый в
# registry «latest» попал бы на все серверы при следующей установке. Обновление — осознанно: сменить дайджест
# (docker image inspect amneziavpn/amneziawg-go:<tag> --format '{{index .RepoDigests 0}}').
AMNEZIAWG_IMAGE = "amneziavpn/amneziawg-go@sha256:cbafc02b8373a83f428272db6d8001b37bc02e6211cbd8c0cb4e2e3759b12b72"
# Фильтр docker ps по имени ищет ПОДСТРОКУ: name=amnezia-awg совпал бы и с чужим amnezia-awg2.
# Якоря ^…$ делают совпадение точным; /? — docker хранит имена с ведущим слэшем.
# В одинарных кавычках: иначе shell принял бы ? за glob-шаблон, а $ — за начало переменной.
CONTAINER_NAME_FILTER = f"'name=^/?{CONTAINER_NAME}$'"

# Каталог с конфигом на хосте и он же внутри контейнера (bind-mount, см. AmneziaWGProvider._run_container).
HOST_CONFIG_DIR = "/opt/vpn-admin/awg"
CONTAINER_CONFIG_DIR = "/etc/amnezia/awg"
CONFIG_FILE_NAME = "awg0.conf"
INTERFACE_NAME = "awg0"

# Параметры конфига клиента.
CLIENT_DNS = "1.1.1.1, 1.0.0.1"
CLIENT_ALLOWED_IPS = "0.0.0.0/0, ::/0"
CLIENT_PERSISTENT_KEEPALIVE = 25

# Выход клиентов в интернет: пакеты из подсети туннеля уходят наружу через eth0 контейнера
# с подменой адреса отправителя (MASQUERADE), иначе ответы не вернутся. Подсеть должна совпадать
# с Address в шаблоне ниже. PostUp идемпотентен (-C проверяет наличие правила перед -A).
TUNNEL_SUBNET = "10.8.1.0/24"


def nat_rule(tunnel_subnet: str) -> str:
    return f"POSTROUTING -s {tunnel_subnet} -o eth0 -j MASQUERADE"


def nat_post_up(tunnel_subnet: str) -> str:
    rule = nat_rule(tunnel_subnet)
    return f"iptables -t nat -C {rule} 2>/dev/null || iptables -t nat -A {rule}"


def nat_post_down(tunnel_subnet: str) -> str:
    return f"iptables -t nat -D {nat_rule(tunnel_subnet)}"


# Значения для подсети по умолчанию — для тестов и совместимости; провайдер берёт их из своей раскладки.
NAT_RULE = nat_rule(TUNNEL_SUBNET)
NAT_POST_UP = nat_post_up(TUNNEL_SUBNET)
NAT_POST_DOWN = nat_post_down(TUNNEL_SUBNET)


@dataclass(frozen=True)
class ContainerLayout:
    """Раскладка контейнера протокола на сервере: имя, папки, интерфейс, подсеть туннеля, DNS клиентов.

    У каждого провайдера она своя (атрибут класса `layout`): обычный протокол панели — DEFAULT_LAYOUT, будущий
    «шлюз» — отдельный контейнер с другим именем, папкой, интерфейсом и подсетью. Все команды провайдера собираются
    только из полей раскладки.
    """

    container_name: str
    host_config_dir: str  # папка конфига на хосте; она же монтируется в контейнер (признак «наш» контейнер)
    container_config_dir: str
    config_file_name: str
    interface_name: str
    tunnel_subnet: str  # в виде сети (10.8.1.0/24): по ней строится правило NAT
    client_dns: str
    # Address серверного интерфейса, если он не совпадает с tunnel_subnet (например, 10.77.0.1/22: сервер занимает .1,
    # клиенты начинаются с .2). None — Address равен tunnel_subnet (как у обычного протокола, сервер на .0).
    server_address: str | None = None

    @property
    def container_filter(self) -> str:
        """Точный фильтр docker ps по имени (см. CONTAINER_NAME_FILTER)."""
        return f"'name=^/?{self.container_name}$'"

    @property
    def interface_address(self) -> str:
        return self.server_address or self.tunnel_subnet

    @property
    def host_config_file(self) -> str:
        return f"{self.host_config_dir}/{self.config_file_name}"

    @property
    def container_config_file(self) -> str:
        return f"{self.container_config_dir}/{self.config_file_name}"

    @property
    def nat_rule(self) -> str:
        return nat_rule(self.tunnel_subnet)

    @property
    def nat_post_up(self) -> str:
        return nat_post_up(self.tunnel_subnet)

    @property
    def nat_post_down(self) -> str:
        return nat_post_down(self.tunnel_subnet)


DEFAULT_LAYOUT = ContainerLayout(
    container_name=CONTAINER_NAME,
    host_config_dir=HOST_CONFIG_DIR,
    container_config_dir=CONTAINER_CONFIG_DIR,
    config_file_name=CONFIG_FILE_NAME,
    interface_name=INTERFACE_NAME,
    tunnel_subnet=TUNNEL_SUBNET,
    client_dns=CLIENT_DNS,
)

# Health: клиент считается активным, если рукопожатие было не раньше этого окна (WireGuard повторяет
# рукопожатие каждые ~2 минуты, пока туннелем пользуются).
ACTIVE_HANDSHAKE_WINDOW_SECONDS = 180
# Проверка выхода сервера в интернет. Команда — константа: адрес никогда не берётся из ввода пользователя.
# time_connect — секунды от старта до установленного TCP-соединения (включая DNS). Не f-строка: %{...} — синтаксис curl.
INTERNET_PROBE_COMMAND = (
    "curl -o /dev/null -s -w '%{time_connect}' --max-time 5 https://www.cloudflare.com/cdn-cgi/trace"
)


# Шаблон серверного конфига. Ключ, порт и параметры обфускации подставляются при установке
# (render_awg_config): обфускация своя у каждого сервера — одинаковые значения на всех серверах
# (и H1..H4 = 1..4, как у обычного WireGuard) делали трафик узнаваемым по общей сигнатуре.
# Фигурные скобки PostUp/PostDown здесь не встречаются — шаблон безопасен для str.format.
def awg_config_template(address: str, tunnel_subnet: str) -> str:
    """Шаблон под раскладку: Address интерфейса и NAT-хуки по подсети туннеля."""
    return (
        "[Interface]\n"
        "PrivateKey = {private_key}\n"
        f"Address = {address}\n"
        "ListenPort = {port}\n"
        f"PostUp = {nat_post_up(tunnel_subnet)}\n"
        f"PostDown = {nat_post_down(tunnel_subnet)}\n"
        "{obfuscation}\n"
        """S3 = 33
S4 = 12
ContentPaddingAddition = 10-100
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = on
DisableCookies = on"""
    )


DEFAULT_AWG_CONFIG = awg_config_template(TUNNEL_SUBNET, TUNNEL_SUBNET)
