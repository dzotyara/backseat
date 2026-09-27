"""The panel against temporary bot databases, written the way the bots write them."""

import asyncio
import warnings
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.exceptions import StarletteDeprecationWarning

from backseat.bot_config import GLOBAL, BotConfig, Runtime
from backseat.config import CoreSettings
from backseat.storage import Storage, StoredMessage
from backseat.web.app import create_app
from backseat.web.bots import HEARTBEAT_KEY
from backseat.web.settings import WebSettings

with warnings.catch_warnings():
    # Starlette 1.7 prefers the httpx2 fork for its TestClient; plain httpx still works.
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from fastapi.testclient import TestClient

NOW = 1_790_000_000.0
CHAT = -1001
PERSONA = "Ты — тестовый бот.\nСтеби Ивана."
BOT_MODELS = ["paid/model", "free/model:free"]
OPENROUTER_KEY = "sk-or-v1-panel-SECRET-0123456789"
# Must never show up on a page: the panel's key, the bots' tokens, the key label OpenRouter echoes back.
SECRETS = (OPENROUTER_KEY, "42:TELEGRAM-SECRET-TOKEN", "DISCORD.SECRET.TOKEN", "sk-or-v1-a1b...LABEL")
KEY_INFO = {
    "label": "sk-or-v1-a1b...LABEL",
    "usage": 12.5,
    "usage_daily": 0.0123,
    "usage_weekly": 0.5,
    "usage_monthly": 1.5,
    "free_model_daily_requests": {"used": 3, "limit": 50},
}


def msg(
    message_id: int,
    text: str,
    *,
    user_id: int = 700,
    author: str = "Иван",
    reply_to: int | None = None,
    is_bot: bool = False,
    chat_id: int = CHAT,
) -> StoredMessage:
    return StoredMessage(
        chat_id=chat_id,
        message_id=message_id,
        user_id=42 if is_bot else user_id,
        author="bot" if is_bot else author,
        text=text,
        reply_to=reply_to,
        is_bot=is_bot,
        created_at=int(NOW) - 86_400 + message_id * 60,
    )


class BotDb:
    """One bot's database file, touched only through the bot's own classes."""

    def __init__(self, path: Path, platform: str, persona_file: Path) -> None:
        self.path = path
        self.platform = platform
        self.settings = CoreSettings(
            _env_file=None,  # type: ignore[call-arg]
            openrouter_api_key="sk-or-v1-bot",
            models=BOT_MODELS,
            bot_names=["бэксит", "ботяра"],
            persona_file=persona_file,
            unprompted_cooldown_seconds=60.0,
            reactions_enabled=True,
            weekly_digest=True,
        )

    def run[T](self, action: Callable[[Storage, BotConfig], Awaitable[T]]) -> T:
        async def go() -> T:
            storage = Storage(self.path)
            await storage.connect()
            try:
                return await action(storage, BotConfig(storage, self.settings, platform=self.platform))
            finally:
                await storage.close()

        return asyncio.run(go())

    def setup(
        self,
        *,
        publish: bool = True,
        heartbeat_age: float | None = 30,
        messages: Iterable[StoredMessage] = (),
        summary: str | None = None,
        meta: dict[str, str] | None = None,
    ) -> None:
        async def go(storage: Storage, config: BotConfig) -> None:
            if publish:
                await config.publish_defaults()
            if heartbeat_age is not None:
                await storage.set_meta(HEARTBEAT_KEY, str(int(NOW - heartbeat_age)))
            for message in messages:
                await storage.add_message(message)
            if summary is not None:
                await storage.set_summary(CHAT, summary, 30, int(NOW) - 3600)
            for key, value in (meta or {}).items():
                await storage.set_meta(key, value)

        self.run(go)

    def runtime(self) -> Runtime:
        return self.run(lambda storage, config: config.runtime())

    def setting(self, key: str) -> str | None:
        return self.run(lambda storage, config: storage.get_setting(GLOBAL, key))


class FakeOpenRouter:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.payload: Any = {"data": KEY_INFO}
        self.error: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json=self.payload)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@dataclass
class Panel:
    client: TestClient
    telegram: BotDb
    discord: BotDb
    openrouter: FakeOpenRouter
    clock: Clock


def make_panel(tmp_path: Path, **settings: Any) -> Panel:
    persona_file = tmp_path / "persona.md"
    persona_file.write_text(PERSONA + "\n", encoding="utf-8")
    telegram = BotDb(tmp_path / "backseat.db", "Telegram", persona_file)
    discord = BotDb(tmp_path / "discord.db", "Discord", persona_file)
    values: dict[str, Any] = {
        "telegram_db_path": telegram.path,
        "discord_db_path": discord.path,
        "openrouter_api_key": OPENROUTER_KEY,
    }
    values.update(settings)
    openrouter, clock = FakeOpenRouter(), Clock()
    app = create_app(
        WebSettings(_env_file=None, **values),  # type: ignore[call-arg]
        openrouter_transport=httpx.MockTransport(openrouter),
        clock=clock,
    )
    return Panel(TestClient(app, base_url="http://localhost"), telegram, discord, openrouter, clock)


@pytest.fixture
def panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Panel:
    # The shared .env also holds the bots' tokens: the panel must neither need nor show them.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRETS[1])
    monkeypatch.setenv("DISCORD_BOT_TOKEN", SECRETS[2])
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_KEY)
    return make_panel(tmp_path)


def card(page: str, slug: str) -> str:
    """One bot's card on the dashboard."""
    start = page.index(f'aria-labelledby="bot-{slug}"')
    return page[start : page.index("</section>", start)]
