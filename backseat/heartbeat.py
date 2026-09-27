"""What a running bot tells the web panel: that it is alive, its defaults, its chats' names."""

import asyncio
import logging
import time

from backseat.bot_config import BotConfig
from backseat.storage import Storage

log = logging.getLogger(__name__)

HEARTBEAT_KEY = "heartbeat"  # meta: unix seconds of the last beat
CHAT_TITLE_PREFIX = "chat_title:"  # meta "chat_title:<chat_id>" -> the chat's name
HEARTBEAT_SECONDS = 60.0


async def beat(storage: Storage, bot_config: BotConfig) -> None:
    await storage.set_meta(HEARTBEAT_KEY, str(int(time.time())))
    # Republished on every beat, so an edited persona file reaches the panel within a minute.
    await bot_config.publish_defaults()


async def run_heartbeat(storage: Storage, bot_config: BotConfig, interval: float = HEARTBEAT_SECONDS) -> None:
    while True:
        try:
            await beat(storage, bot_config)
        except Exception:
            log.exception("Heartbeat failed")
        await asyncio.sleep(interval)


async def remember_chat_title(storage: Storage, chat_id: int, title: str) -> None:
    await storage.set_meta(f"{CHAT_TITLE_PREFIX}{chat_id}", title)
