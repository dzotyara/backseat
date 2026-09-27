"""Stand-ins for discord.py objects. A real discord.Message needs a live connection state, so messages,
channels and interactions are small fakes; attachments, stickers, polls, snapshots and references are
real discord.py objects built from API payloads."""

import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
from discord.message import MessageSnapshot

from backseat.bot_config import BotConfig
from backseat.discord.commands import Services
from backseat.discord.settings import DiscordSettings
from backseat.llm import Completion, LLMError
from backseat.storage import Storage
from backseat.triggers import BotIdentity

# Fake snowflakes: this repository is public.
GUILD = 100000000000000001
CHANNEL = 100000000000000002
THREAD = 100000000000000003
OTHER_CHANNEL = 100000000000000004
BOT_ID = 200000000000000001
BOT_ROLE = 200000000000000002
OWNER = 300000000000000001
PETYA = 300000000000000002
IVAN = 300000000000000003
OTHER_BOT = 300000000000000004

ME = BotIdentity(id=BOT_ID, username="Бэксит", platform="Discord")
BASE = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)  # 15:00 in Moscow

# Payload-built discord.py objects touch state.http only to download something.
_STATE: Any = SimpleNamespace(http=None)
_ids = itertools.count(400000000000000001)


def make_settings(tmp_path: Path, **overrides: Any) -> DiscordSettings:
    persona = tmp_path / "persona.md"
    if not persona.exists():
        persona.write_text("Ты — тестовый бот.", encoding="utf-8")
    values: dict[str, Any] = {
        "discord_bot_token": "test-token",
        "openrouter_api_key": "sk-test",
        "models": ["paid/model", "free/model:free"],
        "db_path": tmp_path / "discord.db",
        "persona_file": persona,
        "allowed_chat_ids": [CHANNEL],
        "owner_ids": [OWNER],
        "focus_users": {IVAN: "Иван"},
        "debounce_seconds": 0.0,
        "addressed_debounce_seconds": 0.0,
        "weekly_digest": False,
    }
    values.update(overrides)
    return DiscordSettings(_env_file=None, **values)


def attachment(filename: str, content_type: str | None = None, **payload: Any) -> discord.Attachment:
    data = {"id": next(_ids), "filename": filename, "size": 100, "url": "https://cdn/x", "proxy_url": "https://cdn/x"}
    if content_type:
        data["content_type"] = content_type
    data.update(payload)
    return discord.Attachment(data=data, state=_STATE)  # type: ignore[arg-type]


def sticker(name: str) -> discord.StickerItem:
    return discord.StickerItem(data={"id": next(_ids), "name": name, "format_type": 1}, state=_STATE)


def make_poll(question: str, *answers: str) -> discord.Poll:
    poll = discord.Poll(question=question, duration=timedelta(hours=1))
    for answer in answers:
        poll.add_answer(text=answer)
    return poll


def snapshot(content: str = "", attachments: list[discord.Attachment] | None = None) -> MessageSnapshot:
    data: Any = {
        "type": 0,
        "content": content,
        "embeds": [],
        "attachments": [],
        "timestamp": BASE.isoformat(),
        "edited_timestamp": None,
        "flags": 0,
    }
    forwarded = MessageSnapshot(_STATE, data)
    forwarded.attachments = attachments or []
    return forwarded


def make_reference(
    message_id: int, *, author_id: int | None = None, channel_id: int = CHANNEL, forward: bool = False
) -> discord.MessageReference:
    """A reference as Discord sends it; author_id makes it resolve to that person's message."""
    kind = discord.MessageReferenceType.forward if forward else discord.MessageReferenceType.reply
    reference = discord.MessageReference(message_id=message_id, channel_id=channel_id, type=kind)
    if author_id is not None:
        reference.resolved = SimpleNamespace(author=SimpleNamespace(id=author_id))  # type: ignore[assignment]
    return reference


def text_channel(channel_id: int = CHANNEL) -> SimpleNamespace:
    return SimpleNamespace(id=channel_id)


def thread(thread_id: int = THREAD, parent_id: int = CHANNEL) -> SimpleNamespace:
    return SimpleNamespace(id=thread_id, parent_id=parent_id)


def make_message(
    content: str = "",
    *,
    message_id: int | None = None,
    author_id: int = PETYA,
    author_name: str = "Петя",
    bot: bool = False,
    webhook_id: int | None = None,
    channel: SimpleNamespace | None = None,
    dm: bool = False,
    attachments: list[discord.Attachment] | None = None,
    stickers: list[discord.StickerItem] | None = None,
    poll: discord.Poll | None = None,
    snapshots: list[MessageSnapshot] | None = None,
    mentions: list[int] | None = None,
    role_mentions: list[int] | None = None,
    reference: discord.MessageReference | None = None,
    system: bool = False,
    created_at: datetime = BASE,
    edited_at: datetime | None = None,
) -> Any:
    """A discord.Message look-alike; content doubles as clean_content."""
    return SimpleNamespace(
        id=message_id or next(_ids),
        content=content,
        clean_content=content,
        author=SimpleNamespace(id=author_id, display_name=author_name, bot=bot),
        webhook_id=webhook_id,
        channel=channel or text_channel(),
        guild=None if dm else SimpleNamespace(id=GUILD, self_role=SimpleNamespace(id=BOT_ROLE)),
        attachments=attachments or [],
        stickers=stickers or [],
        poll=poll,
        message_snapshots=snapshots or [],
        mentions=[SimpleNamespace(id=user_id) for user_id in mentions or []],
        role_mentions=[SimpleNamespace(id=role_id) for role_id in role_mentions or []],
        reference=reference,
        created_at=created_at,
        edited_at=edited_at,
        is_system=lambda: system,
    )


def http_error(kind: type[discord.HTTPException] = discord.Forbidden, status: int = 403) -> discord.HTTPException:
    return kind(SimpleNamespace(status=status, reason="Nope"), "Missing Access")


class FakeChannel(discord.abc.Messageable):
    """Records what the transport and the backfill do with a channel. A Messageable subclass,
    because the transport only accepts channels one can post to."""

    def __init__(
        self,
        channel_id: int = CHANNEL,
        *,
        history: list[Any] | None = None,
        error: discord.HTTPException | None = None,
        typing_error: discord.HTTPException | None = None,
    ) -> None:
        self.id = channel_id
        self.sent: list[SimpleNamespace] = []
        self.reactions: list[tuple[int, str]] = []
        self.history_calls: list[dict[str, Any]] = []
        self.typing_shown = 0
        self._history = history or []
        self._error = error
        self._typing_error = typing_error

    async def send(self, content: str, **kwargs: Any) -> Any:
        if self._error:
            raise self._error
        message = SimpleNamespace(id=next(_ids), created_at=BASE, content=content, **kwargs)
        self.sent.append(message)
        return message

    def get_partial_message(self, message_id: int) -> Any:
        async def add_reaction(emoji: str) -> None:
            if self._error:
                raise self._error
            self.reactions.append((message_id, emoji))

        return SimpleNamespace(add_reaction=add_reaction)

    def typing(self) -> Any:
        @contextlib.asynccontextmanager
        async def indicator() -> AsyncIterator[None]:
            if self._typing_error:
                raise self._typing_error
            self.typing_shown += 1
            yield

        return indicator()

    async def history(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.history_calls.append(kwargs)
        if self._error:
            raise self._error
        for message in self._history:
            yield message


class FakeClient:
    """get_channel/fetch_channel of discord.Client: `cached` channels are in the cache, `fetchable`
    ones only in the API, any other id is a 404."""

    def __init__(self, cached: list[Any] | None = None, fetchable: list[Any] | None = None) -> None:
        self._cached = {channel.id: channel for channel in cached or []}
        self._fetchable = {channel.id: channel for channel in fetchable or []}
        self.fetched: list[int] = []

    def get_channel(self, channel_id: int) -> Any:
        return self._cached.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> Any:
        self.fetched.append(channel_id)
        if channel_id not in self._fetchable:
            raise http_error(discord.NotFound, 404)
        return self._fetchable[channel_id]


class FakeLLM:
    """complete() returns the scripted answers in order, then fails like a dead OpenRouter."""

    def __init__(self, *answers: str) -> None:
        self.models = ["paid/model", "free/model:free"]
        self.last_model: str | None = "paid/model"
        self.answers = list(answers)

    async def complete(self, messages: list[dict[str, str]], **_: Any) -> Completion:
        if not self.answers:
            raise LLMError("no scripted answer")
        return Completion(text=self.answers.pop(0), model="paid/model")

    async def key_info(self) -> dict[str, Any] | None:
        return {"free_model_daily_requests": {"used": 3, "limit": 50}, "usage_daily": 0.0123}

    async def aclose(self) -> None:
        pass


class FakeResponder:
    def __init__(self) -> None:
        self.enqueued: list[tuple[int, Any]] = []
        self.remembered: list[tuple[int, Any, str]] = []

    def enqueue(self, chat_id: int, incoming: Any) -> None:
        self.enqueued.append((chat_id, incoming))

    async def remember(self, chat_id: int, sent: Any, text: str, reply_to: int | None = None) -> None:
        self.remembered.append((chat_id, sent, text))

    async def shutdown(self) -> None:
        pass


class FakeDigest:
    """compose() returns the scripted results in order; an Exception item is raised instead."""

    def __init__(self, *results: str | Exception | None) -> None:
        self.results = list(results)
        self.calls: list[tuple[int, int]] = []
        self.schedules_started = 0

    async def compose(self, chat_id: int, since_ts: int) -> str | None:
        self.calls.append((chat_id, since_ts))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def run_forever(self) -> None:
        self.schedules_started += 1
        await asyncio.Event().wait()


class FakeSummarizer:
    """update_once() returns the scripted results in order, then False; an Exception item is raised."""

    def __init__(self, *results: bool | Exception) -> None:
        self.results = list(results)
        self.calls: list[int] = []
        self.maintained: list[int] = []

    async def update_once(self, chat_id: int) -> bool:
        self.calls.append(chat_id)
        result = self.results.pop(0) if self.results else False
        if isinstance(result, Exception):
            raise result
        return result

    async def maintain(self, chat_id: int) -> None:
        self.maintained.append(chat_id)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_services(settings: DiscordSettings, storage: Storage, **overrides: Any) -> Services:
    values: dict[str, Any] = {
        "settings": settings,
        "storage": storage,
        "bot_config": BotConfig(storage, settings),
        "llm": FakeLLM(),
        "responder": FakeResponder(),
        "digest": FakeDigest(),
        "me": ME,
        "owners": frozenset({OWNER}),
        "clock": Clock(),
    }
    values.update(overrides)
    return Services(**values)


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[SimpleNamespace] = []
        self.deferred: SimpleNamespace | None = None

    async def send_message(self, content: str | None = None, *, ephemeral: bool = False, **kwargs: Any) -> None:
        self.messages.append(SimpleNamespace(content=content, ephemeral=ephemeral, **kwargs))

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.deferred = SimpleNamespace(ephemeral=ephemeral, thinking=thinking)


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[SimpleNamespace] = []

    async def send(self, content: str, *, ephemeral: bool = False, **kwargs: Any) -> SimpleNamespace:
        message = SimpleNamespace(id=next(_ids), created_at=BASE, content=content, ephemeral=ephemeral, **kwargs)
        self.sent.append(message)
        return message


class FakeInteraction:
    def __init__(self, user_id: int = PETYA, channel: SimpleNamespace | None = None) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.channel = channel or text_channel()
        self.channel_id = self.channel.id
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.original_deleted = False

    async def delete_original_response(self) -> None:
        self.original_deleted = True

    def replies(self) -> list[SimpleNamespace]:
        """Everything the caller was shown, in order: the response, then the followups."""
        return self.response.messages + self.followup.sent
