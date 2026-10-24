#!/usr/bin/python
# -*- coding: utf-8 -*-

from typing import Literal

from pydantic import field_validator
from pydantic.types import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    project_name: str = "Tollwire"
    database_url: str
    redis_url: str
    jwt_secret_key: SecretStr
    encryption_key: SecretStr
    jwt_access_expire: int
    jwt_refresh_expire: int
    ssh_connect_timeout: int
    ssh_command_timeout: int
    log_level: str = "info"
    # json — одна JSON-строка на событие (прод, docker-compose.prod.yml); text — читаемо глазами (локально).
    log_format: Literal["json", "text"] = "text"
    # Флаг Secure у refresh-cookie: браузер шлёт её только по HTTPS. В проде — всегда true;
    # false — только для локальной разработки по http://localhost.
    cookie_secure: bool = True
    # /api/openapi (Swagger) и схема. В проде выключено (docker-compose.prod.yml): карту всех эндпоинтов не публикуем.
    docs_enabled: bool = True

    # Ограничение частоты (rate limiting), Redis. Окна в секундах.
    login_ip_max_attempts: int = 10  # любых попыток входа с одного IP за окно
    login_ip_window_seconds: int = 60
    # Блокировка по неудачам — два порога в одном окне. Низкий — по паре (учётка, IP): атакующий запирает
    # только себя. Высокий — по учётке со всех IP: держит перебор с ботнета, а запереть админа снаружи
    # становится на порядок дороже.
    login_max_failures: int = 5  # неудач с одного IP в одну учётку
    login_email_max_failures: int = 50  # неудач в одну учётку со всех IP
    login_lock_seconds: int = 900
    refresh_ip_max_attempts: int = 30
    refresh_ip_window_seconds: int = 60

    # Bot API (/api/v1/bot/*): неудачи авторизации по IP (до поиска ключа в БД), запросы и регистрации по ключу.
    bot_auth_ip_max_failures: int = 30
    bot_auth_ip_window_seconds: int = 60
    bot_key_max_requests: int = 300  # бот опрашивает статус регистрации раз в 2-3 с, очередь - раз в 5 с
    bot_key_window_seconds: int = 60
    bot_registrations_max: int = 10
    bot_registrations_window_seconds: int = 60

    # Периодический планировщик внутри воркера. Интервалы в секундах; 0 — правило выключено.
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 15
    access_rules_interval_seconds: int = 60  # истечение и активация подписок
    session_cleanup_interval_seconds: int = 3600
    health_interval_seconds: int = 600
    sync_interval_seconds: int = 3600
    stale_jobs_interval_seconds: int = 300
    # Дольше любой нормальной задачи (установка с docker pull — до ~10 мин) с запасом.
    stale_job_timeout_seconds: int = 1800
    # PENDING-задачи, которых нет в очереди Redis (Redis перезапустился / enqueue не дошёл), — вернуть в очередь.
    requeue_interval_seconds: int = 300
    requeue_pending_after_seconds: int = 300
    # Хранение истории: завершённые задачи и записи аудита старше N дней удаляются (0 — хранить вечно).
    retention_interval_seconds: int = 86400
    job_retention_days: int = 90
    audit_retention_days: int = 365

    # Список российских префиксов для «России напрямую» на шлюзе (этап 24.4). Адрес - только из настроек и только https.
    geoip_source_url: str = "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-extended-latest"
    geoip_download_max_bytes: int = 20 * 1024 * 1024
    geoip_download_timeout_seconds: int = 120
    geoip_upload_max_bytes: int = 5 * 1024 * 1024
    # Раз в неделю; ставится, только если на панели есть хотя бы один шлюз. 0 - выключено.
    geoip_update_interval_seconds: int = 7 * 24 * 3600

    # Failover выходов шлюза (24.5): проверка раз в минуту (0 - выключено); закреплённый клиент возвращается на свой
    # выход, когда тот непрерывно исправен столько секунд.
    gateway_watch_interval_seconds: int = 60
    gateway_failover_return_after_seconds: int = 600

    # Мониторинг серверов: замер по SSH (только чтение) и свёртка истории по часам.
    metrics_interval_seconds: int = 60
    metrics_rollup_interval_seconds: int = 300
    metrics_raw_retention_days: int = 7
    metrics_hourly_retention_days: int = 90
    metrics_concurrency: int = 5  # сколько серверов опрашивать одновременно

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("geoip_source_url")
    @classmethod
    def _geoip_url_is_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("GEOIP_SOURCE_URL must be an https URL")
        return value

    @field_validator("jwt_secret_key")
    @classmethod
    def _strong_jwt_secret(cls, value: SecretStr) -> SecretStr:
        # Им подписаны токены: со слабым или шаблонным секретом любой подделает токен любого админа.
        secret = value.get_secret_value()
        if len(secret) < 32 or secret.lower().startswith("change_me"):
            raise ValueError("JWT_SECRET_KEY must be at least 32 random characters (openssl rand -hex 32)")
        return value


settings = Settings()
