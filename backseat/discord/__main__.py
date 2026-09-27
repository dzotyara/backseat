import asyncio
import contextlib

from backseat.discord.app import run
from backseat.discord.settings import DiscordSettings
from backseat.logs import setup_logging


def main() -> None:
    settings = DiscordSettings()  # type: ignore[call-arg]  # required fields come from the environment
    # Gateway heartbeats and per-request lines drown out the bot's own decisions.
    setup_logging(settings.log_level, quiet=("discord.gateway", "discord.http", "httpx"))
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C: the cleanup in run() has already happened
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
