"""The overview of both bots and spending, and the pause switch."""

import asyncio
import logging
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from backseat.web import bots
from backseat.web.bots import BotSlot
from backseat.web.forms import text_field
from backseat.web.pages import PanelDep, redirect
from backseat.web.security import local_path

log = logging.getLogger(__name__)
router = APIRouter()


async def _card(slot: BotSlot, now: float) -> bots.Card:
    try:
        return await bots.card(slot, now)
    except sqlite3.Error as exc:
        log.warning("%s: database unavailable: %s", slot.slug, exc)
        return bots.Card(slot, error="База бота сейчас не читается. Обновите страницу через пару секунд.")


@router.get("/")
async def dashboard(request: Request, panel: PanelDep) -> Response:
    now = panel.clock()
    spent, *cards = await asyncio.gather(panel.spending.get(), *(_card(slot, now) for slot in panel.slots.values()))
    return panel.render(request, "dashboard.html", nav="home", cards=cards, spending=spent)


@router.post("/bots/{slug}/pause")
async def pause(request: Request, slug: str, panel: PanelDep) -> Response:
    form = await request.form()
    paused = text_field(form, "paused") == "1"
    async with panel.open_bot(slug) as bot:
        await bot.config.set_runtime(paused=paused)
    log.info("%s bot %s from the panel", slug, "paused" if paused else "resumed")
    title = panel.slots[slug].title
    message = (
        f"{title}: пауза. Бот читает и запоминает чат, но ничего не пишет."
        if paused
        else f"{title}: бот снова пишет в чат."
    )
    return redirect(local_path(text_field(form, "next")) or "/", message)


@router.get("/bots/{slug}")
async def bot_home(slug: str, panel: PanelDep) -> Response:
    async with panel.open_bot(slug):
        return RedirectResponse(f"/bots/{slug}/persona", status_code=303)
