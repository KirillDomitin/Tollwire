#!/usr/bin/python
# -*- coding: utf-8 -*-

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
    # Флаг Secure у refresh-cookie: браузер шлёт её только по HTTPS. В проде — всегда true;
    # false — только для локальной разработки по http://localhost.
    cookie_secure: bool = True

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
