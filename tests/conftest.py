import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from backseat.config import CoreSettings
from backseat.llm import Completion, LLMError
from backseat.render import LineFormatter
from backseat.storage import Storage, StoredMessage
from backseat.transport import Sent
from backseat.triggers import BotIdentity

CHAT = -1001
BOT = BotIdentity(id=42, username="Backseatyara_bot")
IVAN = 700000001
PETYA = 111
OWNER = 1
BASE_TS = int(datetime(2026, 9, 20, 12, 0, tzinfo=UTC).timestamp())


def make_settings(tmp_path: Path, **overrides: object) -> CoreSettings:
    persona = tmp_path / "persona.md"
    if not persona.exists():
        persona.write_text("Ты — тестовый бот. Стеби Ивана.", encoding="utf-8")
    values: dict[str, object] = {
        "openrouter_api_key": "sk-test",
        "models": ["paid/model", "free/model:free"],
        "db_path": tmp_path / "test.db",
        "persona_file": persona,
        "focus_users": {IVAN: "Иван"},
        "owner_ids": [OWNER],
        "debounce_seconds": 0.0,
        "addressed_debounce_seconds": 0.0,
        "weekly_digest": False,
    }
    values.update(overrides)
    return CoreSettings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> CoreSettings:
    return make_settings(tmp_path)


@pytest.fixture
async def storage(settings: CoreSettings) -> AsyncIterator[Storage]:
    store = Storage(settings.db_path)
    await store.connect()
    yield store
    await store.close()


@pytest.fixture
def formatter() -> LineFormatter:
    return LineFormatter(ZoneInfo("Europe/Moscow"), {IVAN: "Иван"})


def msg(
    message_id: int,
    text: str = "текст",
    *,
    user_id: int = PETYA,
    author: str = "Петя",
    ts: int | None = None,
    reply_to: int | None = None,
    is_bot: bool = False,
    chat_id: int = CHAT,
) -> StoredMessage:
    return StoredMessage(
        chat_id=chat_id,
        message_id=message_id,
        user_id=BOT.id if is_bot else user_id,
        author="bot" if is_bot else author,
        text=text,
        reply_to=reply_to,
        is_bot=is_bot,
        created_at=BASE_TS + message_id * 60 if ts is None else ts,
    )


class FakeLLM:
    """Returns scripted answers in order; an Exception item is raised instead."""

    def __init__(self, *answers: str | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[list[dict[str, str]]] = []
        self.options: list[dict[str, object]] = []  # max_tokens, temperature of each call
        self.models = ["paid/model", "free/model:free"]
        self.last_model: str | None = None

    async def complete(self, messages: list[dict[str, str]], **options: object) -> Completion:
        self.calls.append(messages)
        self.options.append(options)
        answer = self.answers.pop(0) if self.answers else LLMError("no scripted answer")
        if isinstance(answer, Exception):
            raise answer
        self.last_model = "paid/model"
        return Completion(text=answer, model="paid/model")

    async def key_info(self) -> dict[str, object] | None:
        return {"free_model_daily_requests": {"used": 3, "limit": 50}, "usage_daily": 0.0123}

    def prompt_text(self, call: int = -1) -> str:
        return "\n".join(part["content"] for part in self.calls[call])


class FakeTransport:
    """Records what the bot posts and reacts; posted messages get ids after `last_message_id`."""

    platform = "Telegram"
    max_length = 4000

    def __init__(self, last_message_id: int = 10_000) -> None:
        self.sent: list[SimpleNamespace] = []
        self.reactions: list[SimpleNamespace] = []
        self._last_id = last_message_id

    async def send(self, chat_id: int, text: str, *, reply_to: int | None = None, notify: bool = False) -> Sent | None:
        self._last_id += 1
        self.sent.append(
            SimpleNamespace(chat_id=chat_id, message_id=self._last_id, text=text, reply_to=reply_to, notify=notify)
        )
        return Sent(self._last_id, int(time.time()))

    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool:
        self.reactions.append(SimpleNamespace(chat_id=chat_id, message_id=message_id, emoji=emoji))
        return True

    def typing(self, chat_id: int) -> AbstractAsyncContextManager[object]:
        return contextlib.nullcontext()
