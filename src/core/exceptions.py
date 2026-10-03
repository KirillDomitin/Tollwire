#!/usr/bin/python
# -*- coding: utf-8 -*-


class AppError(Exception):
    """Базовое исключение приложения."""


class InvalidCredentialsError(AppError): ...


class CredentialNotFound(AppError): ...


class NotFoundError(AppError): ...


class AlreadyExistsError(AppError): ...


class SSHError(AppError): ...


class SSHConnectionError(SSHError): ...


class SSHAuthError(SSHError): ...


class SSHTimeoutError(SSHError): ...


class SSHHostKeyError(SSHError):
    """Ключ SSH-сервера не совпал с закреплённым: возможна подмена сервера (MITM). Не повторяется автоматически."""

    def __init__(self, host: str, expected: str, presented: str | None):
        self.host = host
        self.expected = expected  # отпечаток закреплённого ключа
        self.presented = presented  # отпечаток предъявленного (None — не удалось получить)
        super().__init__(
            f"SSH host key mismatch for {host}: expected {expected}, server presented {presented or 'unknown'}"
        )


class JobError(AppError): ...


class JobNotFound(JobError): ...


class JobStatusError(JobError): ...


class JobTypeError(JobError): ...


class JobExecutionError(JobError): ...


class ServerBusyError(JobError): ...


class InactiveAccessError(AppError):
    """Объект существует, но доступ по нему уже не действует (отозван или истёк)."""


class LiveCredentialExistsError(AppError):
    """Удаление запрещено: на сервере есть живой клиент, его нужно сначала отозвать."""


class ProtocolError(AppError): ...


class UnsupportedProtocolError(ProtocolError): ...


class AddressPoolExhaustedError(ProtocolError):
    """В подсети туннеля не осталось свободных адресов для нового клиента."""
