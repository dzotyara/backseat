from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from backseat.config import CoreSettings


class DiscordSettings(CoreSettings):
    """The shared .env, then Discord overrides from .env.discord. ALLOWED_CHAT_IDS holds channel ids;
    threads inside an allowed channel are allowed too."""

    model_config = SettingsConfigDict(env_file=(".env", ".env.discord"))

    discord_bot_token: SecretStr
    # Not the Telegram bot's memory and persona. The shared .env may set these: override them in .env.discord.
    persona_file: Path = Path("prompts/discord.md")
    db_path: Path = Path("data/discord.db")
    # On the first start the bot reads this many days of each allowed channel's history (0 = off).
    backfill_days: int = 90
    # How often anyone may ask for /digest in one channel.
    digest_cooldown_seconds: int = 600
