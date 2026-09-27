"""Wiring: storage, OpenRouter client, handlers and background jobs."""

import asyncio
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError

from backseat import __version__
from backseat.bot_config import BotConfig
from backseat.context import ContextBuilder
from backseat.digest import WeeklyDigest
from backseat.heartbeat import beat, remember_chat_title, run_heartbeat
from backseat.llm import LLMClient
from backseat.render import LineFormatter
from backseat.responder import Responder
from backseat.storage import Storage
from backseat.summarizer import Summarizer
from backseat.telegram.handlers import Services, create_router, register_commands
from backseat.telegram.settings import TelegramSettings
from backseat.telegram.transport import TelegramTransport
from backseat.triggers import BotIdentity

log = logging.getLogger("backseat")


async def remember_chat_titles(bot: Bot, storage: Storage, chat_ids: list[int]) -> None:
    """Group names for the web panel, which otherwise knows the chats only by id."""
    for chat_id in chat_ids:
        try:
            chat = await bot.get_chat(chat_id)
        except TelegramAPIError as exc:
            log.warning("chat=%s title unavailable: %s", chat_id, exc)
            continue
        if chat.title:
            await remember_chat_title(storage, chat_id, chat.title)


async def run(settings: TelegramSettings) -> None:
    storage = Storage(settings.db_path)
    await storage.connect()
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    transport = TelegramTransport(bot)
    llm = LLMClient(settings)
    background: list[asyncio.Task[None]] = []
    responder: Responder | None = None
    try:
        user = await bot.get_me()
        me = BotIdentity(id=user.id, username=user.username or "")
        formatter = LineFormatter(ZoneInfo(settings.timezone), settings.focus_users)
        bot_config = BotConfig(storage, settings, platform=transport.platform)
        context = ContextBuilder(storage, bot_config, settings, me, formatter)
        summarizer = Summarizer(storage, llm, settings, formatter, bot_config=bot_config)
        responder = Responder(
            transport=transport,
            storage=storage,
            llm=llm,
            context=context,
            settings=settings,
            bot_config=bot_config,
            me=me,
            after_batch=summarizer.maintain,
        )

        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(Services(settings, storage, bot_config, llm, responder, me)))
        await register_commands(bot, settings.owner_ids)
        await beat(storage, bot_config)  # the panel sees the bot and its defaults right away
        await remember_chat_titles(bot, storage, settings.allowed_chat_ids)
        # Always scheduled: the web panel's switch is checked at posting time.
        digest = WeeklyDigest(
            storage=storage, llm=llm, context=context, responder=responder, settings=settings, bot_config=bot_config
        )
        background.append(asyncio.create_task(digest.run_forever(), name="weekly-digest"))
        background.append(asyncio.create_task(run_heartbeat(storage, bot_config), name="heartbeat"))

        log.info("Backseat v%s started as @%s; models: %s", __version__, me.username, ", ".join(settings.models))
        await dispatcher.start_polling(bot, allowed_updates=["message", "edited_message"])
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        if responder is not None:
            await responder.shutdown()
        await llm.aclose()
        await storage.close()
        await bot.session.close()
