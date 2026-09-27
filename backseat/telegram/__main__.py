import asyncio

from backseat.logs import setup_logging
from backseat.telegram.app import run
from backseat.telegram.settings import TelegramSettings


def main() -> None:
    settings = TelegramSettings()  # type: ignore[call-arg]  # required fields come from the environment
    # Per-update and per-request lines drown out the bot's own decisions.
    setup_logging(settings.log_level, quiet=("aiogram.event", "httpx"))
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
