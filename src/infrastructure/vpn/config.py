#!/usr/bin/python
# -*- coding: utf-8 -*-

CONTAINER_NAME = "amnezia-awg"

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

# Шаблон серверного конфига; {} — приватный ключ и порт (подставляются через str.format).
DEFAULT_AWG_CONFIG = (
    "[Interface]\n"
    "PrivateKey = {}\n"
    f"Address = {TUNNEL_SUBNET}\n"
    "ListenPort = {}\n"
    f"PostUp = {NAT_POST_UP}\n"
    f"PostDown = {NAT_POST_DOWN}\n"
    """Jc = 5
Jmin = 10
Jmax = 50
S1 = 116
S2 = 57
S3 = 33
S4 = 12
H1 = 1
H2 = 2
H3 = 3
H4 = 4
ContentPaddingAddition = 10-100
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = on
DisableCookies = on"""
)
