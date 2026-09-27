"""The owner's web panel: personas, pause, behaviour, memory and spending of both bots.

There is no login. The panel listens on 127.0.0.1 and is opened through an SSH tunnel; on top of that
it refuses foreign Host headers (DNS rebinding), POSTs from other sites (CSRF) and being framed, so a
web page the owner happens to visit can't pause a bot or rewrite its persona."""

import asyncio
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

from backseat import __version__
from backseat.web import bots
from backseat.web.bots import Bot, BotSlot
from backseat.web.formatting import ago, format_seconds, money, number, plural
from backseat.web.forms import BEHAVIOUR_FIELDS, RUNTIME_FIELDS, Behaviour, clean_persona, text_field
from backseat.web.settings import WebSettings
from backseat.web.spending import SpendingCache

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
FLASH_COOKIE = "flash"
LATEST_MESSAGES = 50
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
FIELD_TITLES = {
    "names": "имена",
    "models": "модели",
    "unprompted_cooldown_seconds": "пауза между комментариями",
    "reactions_enabled": "реакции",
    "weekly_digest": "итоги недели",
}
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@dataclass(frozen=True, slots=True)
class Flash:
    kind: str  # "ok" | "info" | "error"
    text: str


class PageMissing(Exception):
    def __init__(self, heading: str, message: str) -> None:
        super().__init__(message)
        self.heading = heading
        self.message = message


def _hostname(host: str) -> str:
    """The name in a Host header: "localhost:8090" -> "localhost", "[::1]:8090" -> "::1"."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:].partition("]")[0]
    return host.partition(":")[0]


def _from_other_site(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")  # older browsers: no Sec-Fetch-Site, but an Origin on POST
    return origin is not None and urlsplit(origin).netloc != request.headers.get("host")


def _local_path(value: str) -> str | None:
    """`value` if it is a path on this site: redirects must not lead elsewhere."""
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else None


def _pop_flash(request: Request) -> Flash | None:
    kind, _, text = unquote(request.cookies.get(FLASH_COOKIE, "")).partition(":")
    return Flash(kind, text) if kind in ("ok", "info", "error") and text else None


def redirect(url: str, message: str, kind: str = "ok") -> RedirectResponse:
    """POST -> redirect -> GET, with a message for the next page."""
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(FLASH_COOKIE, quote(f"{kind}:{message}", safe=""), max_age=60, httponly=True, samesite="strict")
    return response


def create_app(
    settings: WebSettings,
    *,
    openrouter_transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """The panel. `openrouter_transport` and `clock` are for tests."""
    tz = ZoneInfo(settings.timezone)
    slots = {
        slot.slug: slot
        for slot in (
            BotSlot("telegram", "Telegram", settings.telegram_db_path),
            BotSlot("discord", "Discord", settings.discord_db_path),
        )
    }
    spending = SpendingCache(settings.openrouter_base_url, settings.openrouter_api_key, openrouter_transport, clock)
    allowed_hosts = _LOOPBACK | ({_hostname(settings.web_host)} - {"", "0.0.0.0", "::"})

    def local(timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, tz)

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(version=__version__, slots=list(slots.values()))
    templates.env.filters.update(
        when=lambda ts: f"{local(ts):%d.%m.%Y %H:%M}",
        clock=lambda ts: f"{local(ts):%H:%M}",
        day=lambda ts: f"{local(ts):%d.%m.%Y}, {WEEKDAYS[local(ts).weekday()]}",
        ago=lambda ts: ago(clock() - ts),
        plural=plural,
        money=money,
        number=number,
        seconds=format_seconds,
    )

    app = FastAPI(title="Backseat", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def local_only(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if _hostname(request.headers.get("host", "")) not in allowed_hosts:
            response: Response = PlainTextResponse(
                "Панель открывается только через SSH-туннель, по адресу localhost.", status_code=400
            )
        elif request.method not in ("GET", "HEAD") and _from_other_site(request):
            response = PlainTextResponse("Запрос пришёл с другого сайта и отклонён.", status_code=403)
        else:
            response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    def render(request: Request, template: str, status: int = 200, **context: Any) -> Response:
        flash = _pop_flash(request)
        response = templates.TemplateResponse(request, template, {"flash": flash, **context}, status_code=status)
        if flash:
            response.delete_cookie(FLASH_COOKIE)
        return response

    async def bot_page(
        request: Request, bot: Bot, template: str, tab: str, status: int = 200, **context: Any
    ) -> Response:
        state = await bots.bot_state(bot, clock())
        return render(request, template, status, nav=bot.slot.slug, tab=tab, state=state, **context)

    @asynccontextmanager
    async def open_bot(slug: str) -> AsyncIterator[Bot]:
        slot = slots.get(slug)
        if slot is None:
            raise PageMissing("Такого бота нет", "Панель знает только Telegram- и Discord-бота.")
        async with bots.open_bot(slot) as bot:
            if bot is None:
                raise PageMissing(
                    f"{slot.title}-бот ещё не настроен",
                    f"Файла базы {slot.db_path} нет. Он появится после первого запуска бота.",
                )
            yield bot

    @app.exception_handler(PageMissing)
    async def page_missing(request: Request, exc: PageMissing) -> Response:
        return render(request, "message.html", 404, heading=exc.heading, message=exc.message)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 404:
            return render(request, "message.html", 404, heading="Страница не найдена", message="Такой страницы нет.")
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def bad_address(request: Request, exc: RequestValidationError) -> Response:
        return render(request, "message.html", 404, heading="Страница не найдена", message="Такой страницы нет.")

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc: sqlite3.Error) -> Response:
        log.warning("Database error on %s: %s", request.url.path, exc)
        return render(
            request,
            "message.html",
            503,
            heading="База недоступна",
            message="База бота сейчас не читается. Обновите страницу через пару секунд.",
        )

    # --- dashboard and the switch ---

    async def card(slot: BotSlot, now: float) -> bots.Card:
        try:
            return await bots.card(slot, now)
        except sqlite3.Error as exc:
            log.warning("%s: database unavailable: %s", slot.slug, exc)
            return bots.Card(slot, error="База бота сейчас не читается. Обновите страницу через пару секунд.")

    @app.get("/")
    async def dashboard(request: Request) -> Response:
        now = clock()
        spent, *cards = await asyncio.gather(spending.get(), *(card(slot, now) for slot in slots.values()))
        return render(request, "dashboard.html", nav="home", cards=cards, spending=spent)

    @app.post("/bots/{slug}/pause")
    async def pause(request: Request, slug: str) -> Response:
        form = await request.form()
        paused = text_field(form, "paused") == "1"
        async with open_bot(slug) as bot:
            await bot.config.set_runtime(paused=paused)
        log.info("%s bot %s from the panel", slug, "paused" if paused else "resumed")
        title = slots[slug].title
        message = (
            f"{title}: пауза. Бот читает и запоминает чат, но ничего не пишет."
            if paused
            else f"{title}: бот снова пишет в чат."
        )
        return redirect(_local_path(text_field(form, "next")) or "/", message)

    @app.get("/bots/{slug}")
    async def bot_home(slug: str) -> Response:
        async with open_bot(slug):
            return RedirectResponse(f"/bots/{slug}/persona", status_code=303)

    # --- persona ---

    @app.get("/bots/{slug}/persona")
    async def persona_page(request: Request, slug: str) -> Response:
        async with open_bot(slug) as bot:
            custom = await bot.config.has_custom_persona()
            text = await bot.config.persona()
            return await bot_page(request, bot, "persona.html", "persona", text=text, custom=custom)

    @app.post("/bots/{slug}/persona")
    async def persona_save(request: Request, slug: str) -> Response:
        text, error = clean_persona(text_field(await request.form(), "persona"))
        async with open_bot(slug) as bot:
            if error:
                custom = await bot.config.has_custom_persona()
                return await bot_page(
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

    @app.get("/bots/{slug}/persona/reset")
    async def persona_reset_page(request: Request, slug: str) -> Response:
        async with open_bot(slug) as bot:
            if not await bot.config.has_custom_persona():
                return redirect(f"/bots/{slug}/persona", "Бот и так использует характер по умолчанию.", "info")
            default = bot.config.default_persona()
            return await bot_page(request, bot, "persona_reset.html", "persona", default=default)

    @app.post("/bots/{slug}/persona/reset")
    async def persona_reset(slug: str) -> Response:
        async with open_bot(slug) as bot:
            await bot.config.set_persona(None)
        log.info("%s persona reset from the panel", slug)
        return redirect(f"/bots/{slug}/persona", "Вернул характер по умолчанию.")

    # --- names and behaviour ---

    async def behaviour_page(request: Request, bot: Bot, form: Behaviour | None = None, status: int = 200) -> Response:
        config = bot.config
        names, runtime, defaults = await config.names(), await config.runtime(), config.default_runtime()
        default_names = config.default_names()
        overridden = {name for name in RUNTIME_FIELDS if getattr(runtime, name) != getattr(defaults, name)}
        if names != default_names:
            overridden.add("names")
        return await bot_page(
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

    @app.get("/bots/{slug}/settings")
    async def settings_page(request: Request, slug: str) -> Response:
        async with open_bot(slug) as bot:
            return await behaviour_page(request, bot)

    @app.post("/bots/{slug}/settings")
    async def settings_save(request: Request, slug: str) -> Response:
        form = Behaviour.parse(await request.form())
        async with open_bot(slug) as bot:
            if form.errors:
                return await behaviour_page(request, bot, form, 400)
            await bot.config.set_names(None if form.names == bot.config.default_names() else form.names)
            await bot.config.set_runtime(
                models=form.models,
                unprompted_cooldown_seconds=form.cooldown,
                reactions_enabled=form.reactions_enabled,
                weekly_digest=form.weekly_digest,
            )
        log.info("%s behaviour saved from the panel", slug)
        return redirect(f"/bots/{slug}/settings", "Сохранено. Бот применит настройки со следующего сообщения.")

    @app.post("/bots/{slug}/settings/reset")
    async def settings_reset(request: Request, slug: str) -> Response:
        field = text_field(await request.form(), "field")
        if field != "all" and field not in BEHAVIOUR_FIELDS:
            return redirect(f"/bots/{slug}/settings", "Непонятно, что сбросить.", "error")
        async with open_bot(slug) as bot:
            if field in ("names", "all"):
                await bot.config.set_names(None)
            if reset := [name for name in RUNTIME_FIELDS if field in (name, "all")]:
                await bot.config.set_runtime(**dict.fromkeys(reset))
        log.info("%s behaviour reset from the panel: %s", slug, field)
        message = (
            "Имена и поведение — снова по умолчанию."
            if field == "all"
            else f"Вернул по умолчанию: {FIELD_TITLES[field]}."
        )
        return redirect(f"/bots/{slug}/settings", message)

    # --- memory ---

    @app.get("/bots/{slug}/memory")
    async def memory_page(request: Request, slug: str) -> Response:
        async with open_bot(slug) as bot:
            chats = await bots.chats(bot.storage)
            authors = {chat.chat_id: await bots.top_authors(bot.storage, chat.chat_id) for chat in chats}
            return await bot_page(request, bot, "memory.html", "memory", chats=chats, authors=authors)

    @app.get("/bots/{slug}/memory/{chat_id}")
    async def chat_page(request: Request, slug: str, chat_id: int) -> Response:
        async with open_bot(slug) as bot:
            found = await bots.chats(bot.storage, chat_id)
            if not found:
                raise PageMissing("Чат не найден", "В памяти этого бота такого чата нет.")
            messages = await bots.latest_messages(bot.storage, chat_id, LATEST_MESSAGES)
            return await bot_page(
                request,
                bot,
                "chat.html",
                "memory",
                chat=found[0],
                summary=await bot.storage.get_summary(chat_id),
                authors=await bots.top_authors(bot.storage, chat_id),
                messages=messages,
                by_id={message.message_id: message for message in messages},
            )

    return app
