"""What moderators asked the bot to do and what it did. Read-only."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from backseat.web import bots
from backseat.web.pages import PanelDep

router = APIRouter()

LATEST_ENTRIES = 200


@router.get("/bots/{slug}/moderation")
async def moderation_page(request: Request, slug: str, panel: PanelDep) -> Response:
    async with panel.open_bot(slug) as bot:
        entries = await bot.storage.latest_moderation(LATEST_ENTRIES)
        chats = {chat.chat_id: chat.name for chat in await bots.chats(bot.storage)}
        return await panel.bot_page(request, bot, "moderation.html", "moderation", entries=entries, chats=chats)
