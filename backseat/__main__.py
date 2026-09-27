import asyncio
import logging

from backseat.app import run
from backseat.config import Settings


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # required fields come from the environment
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Per-update and per-request lines drown out the bot's own decisions.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
