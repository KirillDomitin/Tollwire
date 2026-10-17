#!/usr/bin/python
# -*- coding: utf-8 -*-


class SSHServerError(Exception): ...


class InstallError(SSHServerError): ...


class CommandExecutionError(SSHServerError): ...


class InvalidServerConfigurationError(SSHServerError): ...


class DockerError(SSHServerError): ...


class ForeignContainerError(DockerError):
    """Контейнер с именем панели создан не панелью (скорее всего, приложением AmneziaVPN): панель его не трогает."""


class UnsupportedOperationError(SSHServerError):
    """Операция недоступна для этого провайдера."""
