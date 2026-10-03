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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
