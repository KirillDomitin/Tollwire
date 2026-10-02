#!/usr/bin/python
# -*- coding: utf-8 -*-
from modules.servers.models.server import ServerStatus, VPNServer

# Общее правило для sync и health: как доступность сервера отражается на его статусе.
# NEW/INSTALLING/ERROR не трогаем — это этапы установки, их меняет только установка.


def mark_unreachable(server: VPNServer) -> None:
    server.status = ServerStatus.OFFLINE


def mark_reachable(server: VPNServer, docker_installed: bool) -> None:
    if docker_installed and server.status == ServerStatus.OFFLINE:
        server.status = ServerStatus.READY
