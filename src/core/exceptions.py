#!/usr/bin/python
# -*- coding: utf-8 -*-


class AppError(Exception):
    """Базовое исключение приложения."""


class InvalidCredentialsError(AppError): ...


class NotFoundError(AppError): ...


class AlreadyExistsError(AppError): ...


class SSHError(AppError): ...


class SSHConnectionError(SSHError): ...


class SSHAuthError(SSHError): ...


class SSHTimeoutError(SSHError): ...
