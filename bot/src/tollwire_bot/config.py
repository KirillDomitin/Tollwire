from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки из окружения (compose передаёт их из .env.bot / bot/.env)."""

    telegram_bot_token: SecretStr  # от @BotFather; хранится только у бота
    bot_api_key: SecretStr  # ключ бота в панели (twb_…)
    panel_url: str  # https://домен панели; внутри сети compose допустим http://api:8000
    panel_ca_file: str | None = None  # свой сертификат панели (самоподписанный), в дополнение к штатным
    state_dir: Path = Path("/data")  # том с состоянием диалогов
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.sqlite3"
