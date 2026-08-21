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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()