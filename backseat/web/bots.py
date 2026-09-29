"""What the panel reads from a bot's database. The bots write the same SQLite files from other
processes at the same time, so every request opens a file, runs a few short statements and closes it."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from backseat.bot_config import DEFAULTS_KEY, SETTINGS_FIELDS, BotConfig, Runtime, check_runtime_value
from backseat.config import CoreSettings
from backseat.heartbeat import CHAT_TITLE_PREFIX, HEARTBEAT_KEY
from backseat.storage import Storage

log = logging.getLogger(__name__)

HEARTBEAT_FRESH_SECONDS = 180


@dataclass(frozen=True, slots=True)
class BotSlot:
    slug: str  # in URLs
    title: str  # "Telegram" / "Discord"
    db_path: Path


class PublishedConfig(BotConfig):
    """BotConfig of a bot that runs in another process. The defaults are the ones it published at
    startup (BotConfig.publish_defaults); until it has, the built-in ones and an unknown persona."""

    def __init__(self, storage: Storage, slot: BotSlot, published: str | None) -> None:
        parsed = _parse_defaults(published)
        self.defaults_known = parsed is not None
        platform, settings, self._persona = parsed or (slot.title, CoreSettings.model_construct(), "")
        super().__init__(storage, settings, platform=platform)

    def default_persona(self) -> str:
        return self._persona


def _parse_defaults(raw: str | None) -> tuple[str, CoreSettings, str] | None:
    """(platform, the settings BotConfig reads, persona text) from what the bot published."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        platform, persona, names = data["platform"], data["persona"], data["names"]
        if not isinstance(platform, str) or not isinstance(persona, str):
            raise ValueError("platform and persona must be strings")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("names must be a list of strings")
        # The bot's settings as far as BotConfig needs them. model_construct: no .env of its own here.
        settings = CoreSettings.model_construct(
            bot_names=names,
            # A bot of an older version publishes fewer fields: the rest keep the built-in defaults.
            **{name: check_runtime_value(name, data[name]) for name in SETTINGS_FIELDS if name in data},
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("Ignoring unreadable published defaults (%s): %.200s", exc, raw)
        return None
    return platform, settings, persona


@dataclass(frozen=True, slots=True)
class Bot:
    slot: BotSlot
    storage: Storage
    config: PublishedConfig


@asynccontextmanager
async def open_bot(slot: BotSlot) -> AsyncIterator[Bot | None]:
    """The bot's database for one request, or None when the bot is not set up yet. Never creates
    the file: an empty database would look like a bot that exists."""
    if not slot.db_path.is_file():
        yield None
        return
    storage = Storage(slot.db_path)
    try:
        await storage.connect()  # inside try: a broken file fails after the connection is opened
        yield Bot(slot, storage, PublishedConfig(storage, slot, await storage.get_meta(DEFAULTS_KEY)))
    finally:
        await storage.close()


@dataclass(frozen=True, slots=True)
class Heartbeat:
    status: str  # "online" | "stale" | "missing"
    at: int | None = None


async def heartbeat(storage: Storage, now: float) -> Heartbeat:
    raw = await storage.get_meta(HEARTBEAT_KEY)
    try:
        at = int(float(raw)) if raw else None
    except (ValueError, OverflowError):  # "nan", "inf", garbage
        at = None
    if at is None:
        return Heartbeat("missing")
    return Heartbeat("online" if now - at < HEARTBEAT_FRESH_SECONDS else "stale", at)


@dataclass(frozen=True, slots=True)
class BotState:
    """What every page shows about a bot next to its name."""

    slot: BotSlot
    runtime: Runtime
    heartbeat: Heartbeat
    defaults_known: bool
    # The platform the database belongs to, when it isn't the one this slot expects (swapped paths).
    foreign_platform: str | None


async def bot_state(bot: Bot, now: float) -> BotState:
    platform = bot.config.platform
    return BotState(
        slot=bot.slot,
        runtime=await bot.config.runtime(),
        heartbeat=await heartbeat(bot.storage, now),
        defaults_known=bot.config.defaults_known,
        foreign_platform=platform if platform.casefold() != bot.slot.title.casefold() else None,
    )


@dataclass(frozen=True, slots=True)
class ChatInfo:
    chat_id: int
    title: str | None
    messages: int
    last_message_at: int
    summary_updated_at: int | None

    @property
    def name(self) -> str:
        return self.title or f"Чат {self.chat_id}"


async def chats(storage: Storage, chat_id: int | None = None) -> list[ChatInfo]:
    """Chats in the bot's memory (or just `chat_id`), the most recently active first."""
    summaries = await storage.summary_times()
    titles = await storage.meta_with_prefix(CHAT_TITLE_PREFIX)
    return [
        ChatInfo(chat, titles.get(str(chat)), count, last, summaries.get(chat))
        for chat, count, last in await storage.chat_activity(chat_id)
    ]


@dataclass(frozen=True, slots=True)
class Card:
    """A bot on the dashboard; `state` is None while the bot is not set up (or its database failed)."""

    slot: BotSlot
    state: BotState | None = None
    names: list[str] = field(default_factory=list)
    custom_persona: bool = False
    chats: list[ChatInfo] = field(default_factory=list)
    error: str | None = None

    @property
    def total_messages(self) -> int:
        return sum(chat.messages for chat in self.chats)

    @property
    def last_message_at(self) -> int | None:
        return max((chat.last_message_at for chat in self.chats), default=None)


async def card(slot: BotSlot, now: float) -> Card:
    async with open_bot(slot) as bot:
        if bot is None:
            return Card(slot)
        return Card(
            slot,
            state=await bot_state(bot, now),
            names=await bot.config.names(),
            custom_persona=await bot.config.has_custom_persona(),
            chats=await chats(bot.storage),
        )
