from pydantic import SecretStr

from backseat.config import CoreSettings


class TelegramSettings(CoreSettings):
    telegram_bot_token: SecretStr  # from @BotFather
