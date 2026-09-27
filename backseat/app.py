"""Wiring: storage, OpenRouter client, handlers and background jobs."""

import asyncio
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher

from backseat import __version__
from backseat.chat_config import ChatConfig
from backseat.config import Settings
from backseat.context import ContextBuilder
from backseat.digest import WeeklyDigest
from backseat.handlers import BOT_COMMANDS, Services, create_router
from backseat.llm import LLMClient
from backseat.render import LineFormatter
from backseat.responder import Responder
from backseat.storage import Storage
from backseat.summarizer import Summarizer
from backseat.triggers import BotIdentity

log = logging.getLogger("backseat")


async def run(settings: Settings) -> None:
    storage = Storage(settings.db_path)
    await storage.connect()
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    llm = LLMClient(settings)
    background: list[asyncio.Task[None]] = []
    responder: Responder | None = None
    try:
        user = await bot.get_me()
        me = BotIdentity(id=user.id, username=user.username or "")
        formatter = LineFormatter(ZoneInfo(settings.timezone), settings.focus_users)
        chat_config = ChatConfig(storage, settings)
        context = ContextBuilder(storage, chat_config, settings, me, formatter)
        summarizer = Summarizer(storage, llm, settings, formatter)
        responder = Responder(
            bot=bot,
            storage=storage,
            llm=llm,
            context=context,
            settings=settings,
            me=me,
            after_batch=summarizer.maintain,
        )

        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(Services(settings, storage, chat_config, llm, responder, me)))
        await bot.set_my_commands(BOT_COMMANDS)
        if settings.weekly_digest:
            digest = WeeklyDigest(storage=storage, llm=llm, context=context, responder=responder, settings=settings)
            background.append(asyncio.create_task(digest.run_forever(), name="weekly-digest"))

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
