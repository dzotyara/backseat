"""What a bot remembers: its chats, and one chat's summary and latest messages. Read-only."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from backseat.web import bots
from backseat.web.pages import PageMissing, PanelDep

router = APIRouter()

LATEST_MESSAGES = 50
TOP_AUTHORS = 5  # without chat titles, the most active people are how the owner tells chats apart


@router.get("/bots/{slug}/memory")
async def memory_page(request: Request, slug: str, panel: PanelDep) -> Response:
    async with panel.open_bot(slug) as bot:
        chats = await bots.chats(bot.storage)
        authors = {chat.chat_id: await bot.storage.top_authors(chat.chat_id, TOP_AUTHORS) for chat in chats}
        return await panel.bot_page(request, bot, "memory.html", "memory", chats=chats, authors=authors)


@router.get("/bots/{slug}/memory/{chat_id}")
async def chat_page(request: Request, slug: str, chat_id: int, panel: PanelDep) -> Response:
    async with panel.open_bot(slug) as bot:
        found = await bots.chats(bot.storage, chat_id)
        if not found:
            raise PageMissing("Чат не найден", "В памяти этого бота такого чата нет.")
        messages = await bot.storage.latest_messages(chat_id, LATEST_MESSAGES)
        return await panel.bot_page(
            request,
            bot,
            "chat.html",
            "memory",
            chat=found[0],
            summary=await bot.storage.get_summary(chat_id),
            authors=await bot.storage.top_authors(chat_id, TOP_AUTHORS),
            messages=messages,
            by_id={message.message_id: message for message in messages},
        )
