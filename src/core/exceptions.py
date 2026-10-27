#!/usr/bin/python
# -*- coding: utf-8 -*-


from typing import Any


class AppError(Exception):
    """Базовое исключение приложения."""

    # Несекретные подробности сбоя для Job.result (например, откатилась ли установка ключа). Ставит сервис.
    job_result: dict[str, Any] | None = None


class InvalidCredentialsError(AppError): ...


class AdminRuleError(AppError):
    """Запрещённое изменение админа: себя, последнего активного superadmin."""


class CredentialNotFound(AppError): ...


class NotFoundError(AppError): ...


class AlreadyExistsError(AppError): ...


class InvalidDataError(AppError):
    """Данные корректны по отдельности, но не вместе (например, конец периода раньше начала) — 422."""


class SSHError(AppError): ...


class SSHConnectionError(SSHError): ...


class SSHAuthError(SSHError): ...


class SSHTimeoutError(SSHError): ...


class SSHHostKeyNotPinnedError(SSHError):
    """Вход по паролю на сервер с незакреплённым ключом запрещён: пароль ушёл бы непроверенному хосту."""


class SSHKeyVerificationError(SSHError):
    """Ключ панели записан в authorized_keys, но сервер не пускает по нему: запись снята, учётка не изменилась."""


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


class JobAlreadyRunningError(AppError):
    """Такая же задача по этому серверу ещё не завершена — дубль не ставим."""


class CredentialChangedError(AppError):
    """SSH-учётку сменили, пока задача на неё опиралась: выбор админа важнее, итог задачи не сохраняется."""


class InactiveAccessError(AppError):
    """Объект существует, но доступ по нему уже не действует (отозван или истёк)."""


class LiveCredentialExistsError(AppError):
    """Удаление запрещено: на сервере есть живой клиент, его нужно сначала отозвать."""


class ConfigUnavailableError(AppError):
    """Конфига клиента в панели нет: клиент подхвачен из AmneziaVPN, приватный ключ остался в приложении."""


class ImportNotAllowedError(AppError):
    """Клиентов нельзя импортировать: их нет в последнем отчёте поиска или панель их не поддерживает."""


class GatewayExitError(AppError):
    """Действие с выходом шлюза невозможно в текущем состоянии: шлюз не установлен, выход не подходит, уже добавлен."""


class StateConflictError(AppError):
    """Действие невозможно в текущем состоянии объекта (например, активировать можно только кандидата) - 409."""


class BotAuthError(AppError):
    """Нет ключа бота, он неверный или отозван - 401; по ответу не видно, что именно из этого."""


class ReasonConflictError(AppError):
    """Действие невозможно - 409 с машинным `reason` в теле: по нему бот объясняет человеку, что не так."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RegistrationConflictError(ReasonConflictError):
    """Регистрацию через бота принять нельзя (invite_used, email_taken, ...)."""


class ProtocolError(AppError): ...


class UnsupportedProtocolError(ProtocolError): ...


class AddressPoolExhaustedError(ProtocolError):
    """В подсети туннеля не осталось свободных адресов для нового клиента."""
