"""A bot's names and behaviour: the form, saving it and going back to the defaults field by field."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response

from backseat.bot_config import SETTINGS_FIELDS
from backseat.web.bots import Bot
from backseat.web.forms import BEHAVIOUR_FIELDS, Behaviour, text_field
from backseat.web.pages import Panel, PanelDep, redirect

log = logging.getLogger(__name__)
router = APIRouter()

FIELD_TITLES = {
    "names": "имена",
    "models": "модели",
    "unprompted_cooldown_seconds": "пауза между комментариями",
    "reactions_enabled": "реакции",
    "weekly_digest": "итоги недели",
}


async def _behaviour_page(
    request: Request, panel: Panel, bot: Bot, form: Behaviour | None = None, status: int = 200
) -> Response:
    config = bot.config
    names, runtime, defaults = await config.names(), await config.runtime(), config.default_runtime()
    default_names = config.default_names()
    overridden = {name for name in SETTINGS_FIELDS if getattr(runtime, name) != getattr(defaults, name)}
    if names != default_names:
        overridden.add("names")
    return await panel.bot_page(
        request,
        bot,
        "settings.html",
        "settings",
        status,
        form=form or Behaviour.show(names, runtime),
        defaults=defaults,
        default_names=default_names,
        overridden=overridden,
    )


@router.get("/bots/{slug}/settings")
async def settings_page(request: Request, slug: str, panel: PanelDep) -> Response:
    async with panel.open_bot(slug) as bot:
        return await _behaviour_page(request, panel, bot)


@router.post("/bots/{slug}/settings")
async def settings_save(request: Request, slug: str, panel: PanelDep) -> Response:
    form = Behaviour.parse(await request.form())
    async with panel.open_bot(slug) as bot:
        if form.errors:
            return await _behaviour_page(request, panel, bot, form, 400)
        await bot.config.set_names(None if form.names == bot.config.default_names() else form.names)
        await bot.config.set_runtime(
            models=form.models,
            unprompted_cooldown_seconds=form.cooldown,
            reactions_enabled=form.reactions_enabled,
            weekly_digest=form.weekly_digest,
        )
    log.info("%s behaviour saved from the panel", slug)
    return redirect(f"/bots/{slug}/settings", "Сохранено. Бот применит настройки со следующего сообщения.")


@router.post("/bots/{slug}/settings/reset")
async def settings_reset(request: Request, slug: str, panel: PanelDep) -> Response:
    field = text_field(await request.form(), "field")
    if field != "all" and field not in BEHAVIOUR_FIELDS:
        return redirect(f"/bots/{slug}/settings", "Непонятно, что сбросить.", "error")
    async with panel.open_bot(slug) as bot:
        if field in ("names", "all"):
            await bot.config.set_names(None)
        if reset := [name for name in SETTINGS_FIELDS if field in (name, "all")]:
            await bot.config.set_runtime(**dict.fromkeys(reset))
    log.info("%s behaviour reset from the panel: %s", slug, field)
    message = (
        "Имена и поведение — снова по умолчанию." if field == "all" else f"Вернул по умолчанию: {FIELD_TITLES[field]}."
    )
    return redirect(f"/bots/{slug}/settings", message)
