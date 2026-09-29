"""A bot's names and settings: the form, saving it and going back to the defaults field by field."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response

from backseat.web.bots import Bot
from backseat.web.forms import SettingsForm, show_value, specs_for, text_field
from backseat.web.pages import Panel, PanelDep, redirect

log = logging.getLogger(__name__)
router = APIRouter()


def _titles(bot: Bot) -> dict[str, str]:
    return {"names": "имена", **{spec.name: spec.label.lower() for spec in specs_for(bot.config.platform)}}


async def _settings_page(
    request: Request, panel: Panel, bot: Bot, form: SettingsForm | None = None, status: int = 200
) -> Response:
    config = bot.config
    specs = specs_for(config.platform)
    names, runtime, defaults = await config.names(), await config.runtime(), config.default_runtime()
    default_names = config.default_names()
    overridden = {spec.name for spec in specs if getattr(runtime, spec.name) != getattr(defaults, spec.name)}
    if names != default_names:
        overridden.add("names")
    shown_defaults = {
        spec.name: (
            ("включено" if getattr(defaults, spec.name) else "выключено")
            if spec.kind == "flag"
            else dict(spec.options).get(getattr(defaults, spec.name))
            if spec.kind == "choice"
            else show_value(spec, getattr(defaults, spec.name)) or "пусто"
        )
        for spec in specs
    }
    return await panel.bot_page(
        request,
        bot,
        "settings.html",
        "settings",
        status,
        form=form or SettingsForm.show(specs, names, runtime),
        shown_defaults=shown_defaults,
        default_names=default_names,
        overridden=overridden,
    )


@router.get("/bots/{slug}/settings")
async def settings_page(request: Request, slug: str, panel: PanelDep) -> Response:
    async with panel.open_bot(slug) as bot:
        return await _settings_page(request, panel, bot)


@router.post("/bots/{slug}/settings")
async def settings_save(request: Request, slug: str, panel: PanelDep) -> Response:
    raw = await request.form()
    async with panel.open_bot(slug) as bot:
        form = SettingsForm.parse(specs_for(bot.config.platform), raw)
        if form.errors:
            return await _settings_page(request, panel, bot, form, 400)
        await bot.config.set_names(None if form.names == bot.config.default_names() else form.names)
        await bot.config.set_runtime(**form.values)
    log.info("%s settings saved from the panel: %s", slug, ", ".join(sorted(form.values)))
    return redirect(f"/bots/{slug}/settings", "Сохранено. Бот применит настройки со следующего сообщения.")


@router.post("/bots/{slug}/settings/reset")
async def settings_reset(request: Request, slug: str, panel: PanelDep) -> Response:
    field = text_field(await request.form(), "field")
    async with panel.open_bot(slug) as bot:
        titles = _titles(bot)
        if field != "all" and field not in titles:
            return redirect(f"/bots/{slug}/settings", "Непонятно, что сбросить.", "error")
        if field in ("names", "all"):
            await bot.config.set_names(None)
        if reset := [name for name in titles if name != "names" and field in (name, "all")]:
            await bot.config.set_runtime(**dict.fromkeys(reset))
    log.info("%s settings reset from the panel: %s", slug, field)
    message = "Все настройки — снова по умолчанию." if field == "all" else f"Вернул по умолчанию: {titles[field]}."
    return redirect(f"/bots/{slug}/settings", message)
