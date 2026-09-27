"""Discord wiring: the client, storage, OpenRouter, slash commands and background jobs."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Coroutine
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from backseat import __version__
from backseat.bot_config import BotConfig
from backseat.context import ContextBuilder
from backseat.digest import WeeklyDigest
from backseat.discord.backfill import backfill, fold_history
from backseat.discord.commands import Services, application_owners, register_commands
from backseat.discord.messages import (
    author_of,
    channel_allowed,
    describe_message,
    find_address,
    is_trivial,
    stored_message,
)
from backseat.discord.settings import DiscordSettings
from backseat.discord.transport import DiscordTransport
from backseat.heartbeat import beat, remember_chat_title, run_heartbeat
from backseat.llm import LLMClient
from backseat.render import LineFormatter
from backseat.responder import Incoming, Responder
from backseat.storage import Storage
from backseat.summarizer import Summarizer
from backseat.triggers import BotIdentity

log = logging.getLogger("backseat.discord")


class BackseatClient(discord.Client):
    def __init__(self, settings: DiscordSettings, storage: Storage, llm: LLMClient) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: switch it on in the Developer Portal as well
        # Nothing the bot posts pings anyone unless a send explicitly allows it.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.storage = storage
        self.llm = llm
        self.tree = app_commands.CommandTree(self)
        self.transport = DiscordTransport(self)
        self.bot_config = BotConfig(storage, settings, platform=self.transport.platform)
        self.formatter = LineFormatter(ZoneInfo(settings.timezone), settings.focus_users)
        self.summarizer = Summarizer(
            storage, llm, settings, self.formatter, platform=self.transport.platform, bot_config=self.bot_config
        )
        self.services: Services | None = None  # set in setup_hook, once the bot knows who it is
        self._background: set[asyncio.Task[None]] = set()
        self._catching_up: set[int] = set()  # channels the catch-up is loading and folding right now
        self._started = False

    async def setup_hook(self) -> None:
        # discord.py has logged in and fetched the application info by now, and no event has been
        # dispatched yet: wire everything here, so no handler ever sees a half-built bot.
        assert self.user is not None
        me = BotIdentity(id=self.user.id, username=self.user.display_name, platform=self.transport.platform)
        context = ContextBuilder(self.storage, self.bot_config, self.settings, me, self.formatter)
        responder = Responder(
            transport=self.transport,
            storage=self.storage,
            llm=self.llm,
            context=context,
            settings=self.settings,
            me=me,
            after_batch=self.after_batch,
            bot_config=self.bot_config,
        )
        # Always built and scheduled: /digest writes its posts, and the weekly switch lives in the web
        # panel, checked at posting time.
        digest = WeeklyDigest(
            storage=self.storage,
            llm=self.llm,
            context=context,
            responder=responder,
            settings=self.settings,
            bot_config=self.bot_config,
        )
        owners = frozenset(self.settings.owner_ids) | application_owners(
            self.application or await self.application_info()
        )
        self.services = Services(self.settings, self.storage, self.bot_config, self.llm, responder, digest, me, owners)
        register_commands(self.tree, self.services)
        try:
            synced = await self.tree.sync()
        except discord.HTTPException:
            log.exception("Could not sync the slash commands")
        else:
            log.info("Slash commands synced globally: %s", " ".join(f"/{command.name}" for command in synced))

    async def on_ready(self) -> None:
        svc = self.services
        if svc is None or self._started:
            return  # on_ready fires again whenever the gateway session had to start over
        self._started = True
        log.info(
            "Backseat v%s started on Discord as %s; models: %s",
            __version__,
            svc.me.username,
            ", ".join(self.settings.models),
        )
        await beat(self.storage, self.bot_config)  # the panel sees the bot and its defaults right away
        self._spawn(run_heartbeat(self.storage, self.bot_config), "heartbeat")
        self._spawn(self.catch_up(svc.me.id), "catch-up")
        self._spawn(svc.digest.run_forever(), "weekly-digest")

    async def catch_up(self, me_id: int) -> None:
        """Read each allowed channel's recent history once, then fold whatever is not summarized yet."""
        for chat_id in self.settings.allowed_chat_ids:
            self._catching_up.add(chat_id)
            try:
                channel = await self.transport.channel(chat_id)
                if channel is not None:
                    if name := getattr(channel, "name", None):  # for the web panel, which only knows ids
                        await remember_chat_title(self.storage, chat_id, f"#{name}")
                    await backfill(channel, self.storage, me_id, self.settings.backfill_days)
                    await fold_history(self.summarizer, chat_id)
            except Exception:
                log.exception("channel=%s catch-up failed", chat_id)
            finally:
                self._catching_up.discard(chat_id)

    async def after_batch(self, chat_id: int) -> None:
        # While the catch-up folds a channel's history, the usual per-batch maintenance of that
        # channel would only fold the same chunks a second time.
        if chat_id not in self._catching_up:
            await self.summarizer.maintain(chat_id)

    async def on_message(self, message: discord.Message) -> None:
        svc = self.services
        if svc is None or message.guild is None:
            return  # not wired yet, or a DM: the bot lives in channels
        if not channel_allowed(message.channel, self.settings.allowed_chat_ids):
            return
        if message.author.id == svc.me.id:
            return  # the bot's own messages are stored when sent
        row = stored_message(message, svc.me.id)
        if row is None:
            return  # a system message or nothing to remember
        await self.storage.add_message(row)
        user_id, _, is_bot = author_of(message)
        if is_bot:
            return  # remember other bots' messages, never talk to them
        # Every @mention, name call and reply to the bot gets an answer — even a bare sticker reply.
        addressed = find_address(message, svc.me.id, await self.bot_config.name_pattern()) is not None
        svc.responder.enqueue(message.channel.id, Incoming(message.id, user_id, addressed, is_trivial(message)))

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        # The raw event, unlike on_message_edit, also covers messages missing from discord.py's cache:
        # the backfilled history and everything posted before a restart.
        message = payload.message
        if message.guild is None or message.edited_at is None:
            return  # a DM, or Discord only attached a link preview
        if not channel_allowed(message.channel, self.settings.allowed_chat_ids):
            return
        if text := describe_message(message):
            edited_at = int(message.edited_at.timestamp())
            await self.storage.edit_message(message.channel.id, message.id, text, edited_at)

    async def shutdown(self) -> None:
        """Stop background jobs and pending replies. The caller closes the connection, OpenRouter and storage."""
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.services is not None:
            await self.services.responder.shutdown()

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


async def run(settings: DiscordSettings) -> None:
    storage = Storage(settings.db_path)
    await storage.connect()
    llm = LLMClient(settings)
    client = BackseatClient(settings, storage, llm)
    loop = asyncio.get_running_loop()
    # `docker stop` sends SIGTERM: disconnect cleanly so the cleanup below runs. Windows has no
    # signal handlers in asyncio; Ctrl+C works there anyway.
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGTERM, lambda: loop.create_task(client.close()))
    try:
        async with client:
            await client.start(settings.discord_bot_token.get_secret_value())
    finally:
        await client.shutdown()
        await llm.aclose()
        await storage.close()
