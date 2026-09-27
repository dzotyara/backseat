from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSettings(BaseSettings):
    """The panel has no login: it listens on one host/port that must stay reachable only through an SSH tunnel."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",  # the shared .env also holds the bots' settings and tokens: never read them
    )

    # A missing file means that bot is not set up yet; the panel never creates it.
    telegram_db_path: Path = Path("data/backseat.db")
    discord_db_path: Path = Path("data/discord.db")
    # Only for the spending block; without it the panel works, just shows no spending.
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    web_host: str = "127.0.0.1"
    web_port: int = 8090
    timezone: str = "Europe/Moscow"
