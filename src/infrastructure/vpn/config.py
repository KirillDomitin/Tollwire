#!/usr/bin/python
# -*- coding: utf-8 -*-

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
NAT_RULE = f"POSTROUTING -s {TUNNEL_SUBNET} -o eth0 -j MASQUERADE"
NAT_POST_UP = f"iptables -t nat -C {NAT_RULE} 2>/dev/null || iptables -t nat -A {NAT_RULE}"
NAT_POST_DOWN = f"iptables -t nat -D {NAT_RULE}"

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
DEFAULT_AWG_CONFIG = (
    "[Interface]\n"
    "PrivateKey = {private_key}\n"
    f"Address = {TUNNEL_SUBNET}\n"
    "ListenPort = {port}\n"
    f"PostUp = {NAT_POST_UP}\n"
    f"PostDown = {NAT_POST_DOWN}\n"
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
