#!/usr/bin/python
# -*- coding: utf-8 -*-
import re
from uuid import UUID

# Имя внутреннего клиента на выходе. По нему sync отличает клиентов шлюза от клиентов панели (`grant-`) и от чужих
# peer'ов. Имя строится только из id строки выхода: повтор после сбоя находит прежнего клиента по имени.
EXIT_CLIENT_PREFIX = "gw-"
_EXIT_CLIENT_NAME = re.compile(r"gw-[0-9a-f]{12}")


def exit_client_name(exit_id: UUID) -> str:
    return f"{EXIT_CLIENT_PREFIX}{exit_id.hex[:12]}"


def is_exit_client_name(name: str | None) -> bool:
    """Имя в точном формате `gw-` + 12 hex. Любое другое `gw-*` (например, `gw-office` из приложения AmneziaVPN) -
    чужое: sync его не трогает. fullmatch, а не match: `$` пропускает завершающий перевод строки."""
    return name is not None and _EXIT_CLIENT_NAME.fullmatch(name) is not None
