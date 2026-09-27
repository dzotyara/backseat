"""A bot's persona: the editor and going back to the default."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response

from backseat.web.formatting import number, plural
from backseat.web.forms import clean_persona, text_field
from backseat.web.pages import PanelDep, redirect

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/bots/{slug}/persona")
async def persona_page(request: Request, slug: str, panel: PanelDep) -> Response:
    async with panel.open_bot(slug) as bot:
        custom = await bot.config.has_custom_persona()
        text = await bot.config.persona()
        return await panel.bot_page(request, bot, "persona.html", "persona", text=text, custom=custom)


@router.post("/bots/{slug}/persona")
async def persona_save(request: Request, slug: str, panel: PanelDep) -> Response:
    text, error = clean_persona(text_field(await request.form(), "persona"))
    async with panel.open_bot(slug) as bot:
        if error:
            custom = await bot.config.has_custom_persona()
            return await panel.bot_page(
                request, bot, "persona.html", "persona", 400, text=text, custom=custom, error=error
            )
        if text == bot.config.default_persona().strip():
            await bot.config.set_persona(None)  # not a custom persona: keep following the persona file
            message = "Текст совпадает со стандартным, так что бот просто использует характер по умолчанию."
        else:
            await bot.config.set_persona(text)
            size = f"{number(len(text))} {plural(len(text), 'символ', 'символа', 'символов')}"
            message = f"Характер сохранён ({size}). Бот применит его со следующего ответа."
    log.info("%s persona saved from the panel: %d chars", slug, len(text))
    return redirect(f"/bots/{slug}/persona", message)


@router.get("/bots/{slug}/persona/reset")
async def persona_reset_page(request: Request, slug: str, panel: PanelDep) -> Response:
    async with panel.open_bot(slug) as bot:
        if not await bot.config.has_custom_persona():
            return redirect(f"/bots/{slug}/persona", "Бот и так использует характер по умолчанию.", "info")
        default = bot.config.default_persona()
        return await panel.bot_page(request, bot, "persona_reset.html", "persona", default=default)


@router.post("/bots/{slug}/persona/reset")
async def persona_reset(slug: str, panel: PanelDep) -> Response:
    async with panel.open_bot(slug) as bot:
        await bot.config.set_persona(None)
    log.info("%s persona reset from the panel", slug)
    return redirect(f"/bots/{slug}/persona", "Вернул характер по умолчанию.")
