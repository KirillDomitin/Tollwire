#!/usr/bin/python
# -*- coding: utf-8 -*-


class SSHServerError(Exception): ...


class InstallError(SSHServerError): ...


class CommandExecutionError(SSHServerError): ...


class InvalidServerConfigurationError(SSHServerError): ...


class DockerError(SSHServerError): ...


class ContainerUpdateError(DockerError):
    """Sanitized update failure; job_result contains image identifiers and rollback status only."""

    def __init__(self, message: str, job_result: dict[str, object]):
        super().__init__(message)
        self.job_result = job_result


class ConfigChangedError(CommandExecutionError):
    """Конфиг на сервере изменился между чтением и записью (например, приложением AmneziaVPN): запись отменена."""


class ForeignContainerError(DockerError):
    """Контейнер с именем панели создан не панелью (скорее всего, приложением AmneziaVPN): панель его не трогает."""


class UnsupportedOperationError(SSHServerError):
    """Операция недоступна для этого провайдера."""
