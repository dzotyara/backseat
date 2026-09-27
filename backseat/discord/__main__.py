import asyncio
import contextlib
import logging

from backseat.discord.app import run
from backseat.discord.settings import DiscordSettings


def main() -> None:
    settings = DiscordSettings()  # type: ignore[call-arg]  # required fields come from the environment
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Gateway heartbeats and per-request lines drown out the bot's own decisions.
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C: the cleanup in run() has already happened
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
