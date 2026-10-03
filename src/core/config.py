#!/usr/bin/python
# -*- coding: utf-8 -*-

from typing import Literal

from pydantic.types import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    project_name: str = "AmneziaAdminPanel"
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

    # Ограничение частоты (rate limiting), Redis. Окна в секундах.
    login_ip_max_attempts: int = 10  # любых попыток входа с одного IP за окно
    login_ip_window_seconds: int = 60
    login_max_failures: int = 5  # неудачных входов в одну учётку (с любых IP) → вход в неё закрыт до конца окна
    login_lock_seconds: int = 900
    refresh_ip_max_attempts: int = 30
    refresh_ip_window_seconds: int = 60

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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
