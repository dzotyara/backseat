"""The owner's web panel: personas, pause, behaviour, memory and spending of both bots.
It has no login; security.py is what keeps other web pages out."""

import time
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backseat.web import pages, security
from backseat.web.bots import BotSlot
from backseat.web.routes import ROUTERS
from backseat.web.settings import WebSettings
from backseat.web.spending import SpendingCache


def create_app(
    settings: WebSettings,
    *,
    openrouter_transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """The panel. `openrouter_transport` and `clock` are for tests."""
    slots = {
        slot.slug: slot
        for slot in (
            BotSlot("telegram", "Telegram", settings.telegram_db_path),
            BotSlot("discord", "Discord", settings.discord_db_path),
        )
    }
    app = FastAPI(title="Backseat", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.panel = pages.Panel(
        slots=slots,
        templates=pages.make_templates(list(slots.values()), ZoneInfo(settings.timezone), clock),
        spending=SpendingCache(settings.openrouter_base_url, settings.openrouter_api_key, openrouter_transport, clock),
        clock=clock,
        timezone=ZoneInfo(settings.timezone),
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.middleware("http")(security.local_only(security.allowed_hosts(settings.web_host)))
    pages.add_error_pages(app)
    for router in ROUTERS:
        app.include_router(router)
    return app
