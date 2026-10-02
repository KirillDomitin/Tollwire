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
