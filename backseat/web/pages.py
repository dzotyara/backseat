"""What every page of the panel shares: the bots it knows, the templates, a message after a POST and
the pages shown instead of an error."""

import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

from backseat import __version__
from backseat.render import WEEKDAYS
from backseat.web import bots
from backseat.web.bots import Bot, BotSlot
from backseat.web.formatting import ago, format_seconds, money, number, plural
from backseat.web.spending import SpendingCache

log = logging.getLogger(__name__)

FLASH_COOKIE = "flash"


@dataclass(frozen=True, slots=True)
class Flash:
    kind: str  # "ok" | "info" | "error"
    text: str


class PageMissing(Exception):
    def __init__(self, heading: str, message: str) -> None:
        super().__init__(message)
        self.heading = heading
        self.message = message


def redirect(url: str, message: str, kind: str = "ok") -> RedirectResponse:
    """POST -> redirect -> GET, with a message for the next page."""
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(FLASH_COOKIE, quote(f"{kind}:{message}", safe=""), max_age=60, httponly=True, samesite="strict")
    return response


def _pop_flash(request: Request) -> Flash | None:
    kind, _, text = unquote(request.cookies.get(FLASH_COOKIE, "")).partition(":")
    return Flash(kind, text) if kind in ("ok", "info", "error") and text else None


def make_templates(slots: list[BotSlot], tz: ZoneInfo, clock: Callable[[], float]) -> Jinja2Templates:
    def local(timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, tz)

    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    templates.env.globals.update(version=__version__, slots=slots)
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
    return templates


@dataclass(frozen=True, slots=True)
class Panel:
    """What the pages work with; create_app keeps it in app.state and PanelDep hands it to a route."""

    slots: dict[str, BotSlot]  # by slug
    templates: Jinja2Templates
    spending: SpendingCache
    clock: Callable[[], float]  # unix seconds; a fake one in tests

    def render(self, request: Request, template: str, status: int = 200, **context: Any) -> Response:
        flash = _pop_flash(request)
        response = self.templates.TemplateResponse(request, template, {"flash": flash, **context}, status_code=status)
        if flash:
            response.delete_cookie(FLASH_COOKIE)
        return response

    async def bot_page(
        self, request: Request, bot: Bot, template: str, tab: str, status: int = 200, **context: Any
    ) -> Response:
        """A page under one bot's tabs, with its heartbeat and switch on top."""
        state = await bots.bot_state(bot, self.clock())
        return self.render(request, template, status, nav=bot.slot.slug, tab=tab, state=state, **context)

    @asynccontextmanager
    async def open_bot(self, slug: str) -> AsyncIterator[Bot]:
        """The bot for one request. PageMissing if the panel knows no such bot or it was never started."""
        slot = self.slots.get(slug)
        if slot is None:
            raise PageMissing("Такого бота нет", "Панель знает только Telegram- и Discord-бота.")
        async with bots.open_bot(slot) as bot:
            if bot is None:
                raise PageMissing(
                    f"{slot.title}-бот ещё не настроен",
                    f"Файла базы {slot.db_path} нет. Он появится после первого запуска бота.",
                )
            yield bot


def _panel(request: Request) -> Panel:
    return request.app.state.panel


PanelDep = Annotated[Panel, Depends(_panel)]


def add_error_pages(app: FastAPI) -> None:
    """Missing pages and an unreadable database get a page of their own instead of a bare error."""

    @app.exception_handler(PageMissing)
    async def page_missing(request: Request, exc: PageMissing) -> Response:
        return _panel(request).render(request, "message.html", 404, heading=exc.heading, message=exc.message)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 404:
            return _not_found(request)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def bad_address(request: Request, exc: RequestValidationError) -> Response:
        return _not_found(request)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc: sqlite3.Error) -> Response:
        log.warning("Database error on %s: %s", request.url.path, exc)
        return _panel(request).render(
            request,
            "message.html",
            503,
            heading="База недоступна",
            message="База бота сейчас не читается. Обновите страницу через пару секунд.",
        )


def _not_found(request: Request) -> Response:
    return _panel(request).render(
        request, "message.html", 404, heading="Страница не найдена", message="Такой страницы нет."
    )
