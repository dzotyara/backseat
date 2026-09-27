"""End-to-end through aiogram's Dispatcher with the Telegram API replaced by a recording session."""

import asyncio
import contextlib
import itertools
import time
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    GetChat,
    GetFile,
    GetMe,
    SendChatAction,
    SendDocument,
    SendMessage,
    SetMessageReaction,
    SetMyCommands,
    TelegramMethod,
)
from aiogram.types import Chat, File, Message, User
from pydantic import ValidationError

from backseat import __main__ as legacy_entrypoint
from backseat.bot_config import BotConfig
from backseat.context import ContextBuilder
from backseat.render import LineFormatter
from backseat.responder import Responder
from backseat.storage import Storage
from backseat.telegram import __main__ as telegram_entrypoint
from backseat.telegram.handlers import Services, create_router, register_commands
from backseat.telegram.settings import TelegramSettings
from backseat.telegram.transport import TelegramTransport
from tests.conftest import BASE_TS, BOT, CHAT, IVAN, OWNER, FakeLLM, make_settings

_ids = itertools.count(100)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.file_bytes = b""
        self.rejected: set[type[TelegramMethod[Any]]] = set()  # methods Telegram answers with an error

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.requests.append(method)
        if type(method) in self.rejected:
            raise TelegramBadRequest(method=method, message="Bad Request: rejected by the test")
        if isinstance(method, GetMe):
            return User(id=BOT.id, is_bot=True, first_name="Backseat", username=BOT.username)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path="documents/prompt.txt")
        if isinstance(method, GetChat):
            return SimpleNamespace(id=method.chat_id, title="Чат")
        if isinstance(method, SendMessage | SendDocument):
            return Message(
                message_id=next(_ids),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="supergroup"),  # type: ignore[arg-type]
                from_user=User(id=BOT.id, is_bot=True, first_name="Backseat"),
                text=getattr(method, "text", None),
            )
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield self.file_bytes

    async def close(self) -> None:
        pass

    def of[M: TelegramMethod[Any]](self, kind: type[M]) -> list[M]:
        return [m for m in self.requests if isinstance(m, kind)]

    def texts(self) -> list[str]:
        return [m.text for m in self.of(SendMessage)]


@contextlib.asynccontextmanager
async def running_app(tmp_path: Path, **overrides: Any) -> AsyncIterator[SimpleNamespace]:
    """The Telegram side wired as in app.py; batches are processed only when a test calls process()."""
    slow = {"debounce_seconds": 999, "addressed_debounce_seconds": 999, "max_batch_wait_seconds": 999}
    settings = make_settings(tmp_path, **{**slow, **overrides})
    storage = Storage(settings.db_path)
    await storage.connect()
    session = RecordingSession()
    bot = Bot("42:TEST", session=session)
    transport = TelegramTransport(bot)
    llm = FakeLLM()
    bot_config = BotConfig(storage, settings)
    formatter = LineFormatter(ZoneInfo(settings.timezone), settings.focus_users)
    context = ContextBuilder(storage, bot_config, settings, BOT, formatter)
    responder = Responder(
        transport=transport,
        storage=storage,
        llm=llm,  # type: ignore[arg-type]
        context=context,
        settings=settings,
        bot_config=bot_config,
        me=BOT,
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(Services(settings, storage, bot_config, llm, responder, BOT)))  # type: ignore[arg-type]
    try:
        yield SimpleNamespace(
            bot=bot,
            dp=dispatcher,
            session=session,
            transport=transport,
            storage=storage,
            llm=llm,
            responder=responder,
            bot_config=bot_config,
        )
    finally:
        await responder.shutdown()
        await storage.close()


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    async with running_app(tmp_path, allowed_chat_ids=[CHAT]) as running:
        yield running


async def feed(
    app: SimpleNamespace,
    text: str | None = None,
    *,
    user_id: int = IVAN,
    chat_id: int = CHAT,
    edited: bool = False,
    message_id: int | None = None,
    is_bot: bool = False,
    **fields: Any,
) -> int:
    message_id = message_id or next(_ids)
    private = chat_id > 0
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": BASE_TS,
        "chat": {"id": chat_id, "type": "private", "first_name": "Я"}
        if private
        else {"id": chat_id, "type": "supergroup", "title": "Чат"},
        "from": {"id": user_id, "is_bot": is_bot, "first_name": "Владелец" if user_id == OWNER else "Иван"},
        **fields,
    }
    if text is not None:
        message["text"] = text
    if edited:
        message["edit_date"] = BASE_TS + 60
    await app.dp.feed_raw_update(app.bot, {"update_id": next(_ids), "edited_message" if edited else "message": message})
    return message_id


async def test_group_message_is_remembered_and_a_call_by_name_is_answered(app: SimpleNamespace) -> None:
    app.llm.answers = ["Слушаю, Иван."]
    first = await feed(app, "всем привет")
    second = await feed(app, "ботяра, ты тут?")
    stored = await app.storage.get_message(CHAT, first)
    assert stored is not None and stored.author == "Иван" and stored.text == "всем привет"

    await app.responder.process(CHAT)
    assert app.session.texts() == ["Слушаю, Иван."]
    reply = app.session.of(SendMessage)[0]
    assert reply.reply_parameters.message_id == second
    # The model sees the chat numbered from #1, not Telegram's message ids.
    assert "К тебе обратились в сообщении #2 (автор — Иван[700000001])" in app.llm.prompt_text()
    [remembered] = await app.storage.messages_after(CHAT, second, 10)
    assert (remembered.is_bot, remembered.reply_to, remembered.text) == (True, second, "Слушаю, Иван.")


async def test_a_bare_sticker_reply_to_the_bot_is_answered_too(app: SimpleNamespace) -> None:
    app.llm.answers = ["Рад, что зашло."]
    bot_message = {
        "message_id": 7,
        "date": BASE_TS,
        "chat": {"id": CHAT, "type": "supergroup", "title": "Чат"},
        "from": {"id": BOT.id, "is_bot": True, "first_name": "Backseat"},
        "text": "шутка",
    }
    sticker = {
        "file_id": "s",
        "file_unique_id": "su",
        "type": "regular",
        "width": 1,
        "height": 1,
        "is_animated": False,
        "is_video": False,
        "emoji": "😂",
    }
    reply = await feed(app, sticker=sticker, reply_to_message=bot_message)
    await app.responder.process(CHAT)
    sent = app.session.of(SendMessage)[0]
    assert (sent.text, sent.reply_parameters.message_id) == ("Рад, что зашло.", reply)


async def test_unprompted_reply_lands_on_the_message_the_model_numbered(app: SimpleNamespace) -> None:
    question = await feed(app, "кто на шашлыки в субботу?")
    await feed(app, "я, но без машины")
    app.llm.answers = ["REPLY #1\nА мясо кто везёт?"]
    await app.responder.process(CHAT)
    reply = app.session.of(SendMessage)[0]
    assert (reply.text, reply.reply_parameters.message_id) == ("А мясо кто везёт?", question)


async def test_unprompted_reaction_lands_on_the_message_the_model_numbered(app: SimpleNamespace) -> None:
    boast = await feed(app, "я сегодня пробежал 10 км")
    await feed(app, "ну или 5")
    app.llm.answers = ["REACT #1 🤡"]
    await app.responder.process(CHAT)
    [reaction] = app.session.of(SetMessageReaction)
    assert (reaction.chat_id, reaction.message_id, reaction.reaction[0].emoji) == (CHAT, boast, "🤡")
    assert app.session.texts() == []


async def test_owner_renames_the_bot_in_cyrillic(app: SimpleNamespace) -> None:
    await feed(app, "/имена бэксит, бот", user_id=OWNER)
    assert app.session.texts()[-1] == "Теперь откликаюсь на: бэксит, бот и @Backseatyara_bot"
    assert await app.bot_config.names() == ["бэксит", "бот"]

    app.llm.answers = ["Тут я."]
    await feed(app, "бот, ответь")
    await app.responder.process(CHAT)
    assert app.session.texts()[-1] == "Тут я."

    await feed(app, "/имена ботище", user_id=IVAN)
    assert app.session.texts()[-1] == "Это может менять только владелец бота."
    await feed(app, "/names сброс", user_id=OWNER)
    assert await app.bot_config.names() == ["бэксит", "ботяра"]


async def test_prompt_does_not_exist_for_anyone_but_the_owner(app: SimpleNamespace) -> None:
    await feed(app, "/промпт")
    await feed(app, "/промпт Ты — злой бот")
    await feed(app, "/prompt", chat_id=IVAN)  # not even in private
    assert app.session.texts() == []
    assert not await app.bot_config.has_custom_persona()


async def test_owner_manages_the_prompt_only_in_private(app: SimpleNamespace) -> None:
    await feed(app, "/prompt Ты — добрый бот", user_id=OWNER)  # in the group: never show or change it there
    assert app.session.texts()[-1] == "Промпт настраивается только у меня в личке."
    assert not await app.bot_config.has_custom_persona()

    await feed(app, "/prompt Ты — добрый бот", user_id=OWNER, chat_id=OWNER)
    assert await app.bot_config.persona() == "Ты — добрый бот"
    await feed(app, "/промпт", user_id=OWNER, chat_id=OWNER)
    assert app.session.texts()[-1] == "Свой характер:\n\nТы — добрый бот"
    await feed(app, "/промпт сброс", user_id=OWNER, chat_id=OWNER)
    assert await app.bot_config.persona() == "Ты — тестовый бот. Стеби Ивана."


async def test_prompt_from_a_text_file(app: SimpleNamespace) -> None:
    app.session.file_bytes = "Характер из файла".encode()
    document = {"file_id": "d1", "file_unique_id": "u1", "file_name": "bot.txt", "mime_type": "text/plain"}
    await feed(app, caption="/промпт", document=document, user_id=OWNER, chat_id=OWNER)
    assert await app.bot_config.persona() == "Характер из файла"
    assert app.session.texts()[-1] == "Новый характер сохранён: 17 символов."


async def test_long_prompt_is_shown_as_a_file(app: SimpleNamespace) -> None:
    await app.bot_config.set_persona("длинно " * 1000)
    await feed(app, "/prompt", user_id=OWNER, chat_id=OWNER)
    assert len(app.session.of(SendDocument)) == 1


async def test_owner_commands_are_only_in_the_owners_menu_and_help(app: SimpleNamespace) -> None:
    await register_commands(app.bot, [OWNER])
    public, owner = app.session.of(SetMyCommands)
    assert public.scope is None
    assert {"prompt", "status"}.isdisjoint(c.command for c in public.commands)
    assert owner.scope.chat_id == OWNER
    assert {"prompt", "status"} <= {c.command for c in owner.commands}

    await feed(app, "/help")
    assert "/prompt" not in app.session.texts()[-1] and "/status" not in app.session.texts()[-1]
    await feed(app, "/help", user_id=OWNER)  # the owner in the group: nothing private is advertised
    assert "/prompt" not in app.session.texts()[-1] and "/status" not in app.session.texts()[-1]
    await feed(app, "/help", user_id=OWNER, chat_id=OWNER)
    assert "/prompt" in app.session.texts()[-1] and "/status" in app.session.texts()[-1]


async def test_id_and_ping(app: SimpleNamespace) -> None:
    await feed(app, "/id", user_id=OWNER)
    assert app.session.texts()[-1] == f"Твой Telegram ID: {OWNER}\nID этого чата: {CHAT}"
    await feed(app, "/ping@Backseatyara_bot")
    assert app.session.texts()[-1] == "pong"


async def test_status_is_owner_only(app: SimpleNamespace) -> None:
    await feed(app, "что-то в чате")
    await feed(app, "/status")
    await feed(app, "/статус", chat_id=IVAN)
    assert app.session.texts() == []

    await feed(app, "/status", user_id=OWNER)
    in_group = app.session.texts()[-1]
    assert "Модели по порядку: paid/model → free/model:free" in in_group
    assert "Память этого чата: 1 сообщений, сводки пока нет" in in_group
    assert "Бесплатные запросы сегодня: 3 из 50" in in_group

    await feed(app, "/status", user_id=OWNER, chat_id=OWNER)
    assert "Память чата «Чат»: 1 сообщений" in app.session.texts()[-1]


async def test_status_in_private_covers_every_chat_the_bot_remembers(tmp_path: Path) -> None:
    async with running_app(tmp_path) as app:  # no ALLOWED_CHAT_IDS: the chats come from memory
        await feed(app, "что-то в чате")
        await feed(app, "а это другой чат", chat_id=-2002)
        await feed(app, "/status", user_id=OWNER, chat_id=OWNER)
        assert app.session.texts()[-1].count("Память чата «Чат»: 1 сообщений") == 2


async def test_foreign_chats_and_other_bots_are_ignored(app: SimpleNamespace) -> None:
    foreign = await feed(app, "ботяра, привет", chat_id=-999)
    assert await app.storage.get_message(-999, foreign) is None

    from_bot = await feed(app, "ботяра, я тоже бот", is_bot=True)
    assert await app.storage.get_message(CHAT, from_bot) is not None  # remembered...
    await app.responder.process(CHAT)
    assert app.llm.calls == []  # ...but never answered


async def test_edits_update_memory(app: SimpleNamespace) -> None:
    message_id = await feed(app, "я бегаю каждый день")
    await feed(app, "я бегаю иногда", message_id=message_id, edited=True)
    stored = await app.storage.get_message(CHAT, message_id)
    assert stored is not None and stored.text == "я бегаю иногда"


async def test_private_chat_gets_a_hint(app: SimpleNamespace) -> None:
    await feed(app, "привет", chat_id=OWNER, user_id=OWNER)
    assert "/help" in app.session.texts()[-1]


async def test_transport_replies_even_if_the_target_is_gone(app: SimpleNamespace) -> None:
    sent = await app.transport.send(CHAT, "ответ", reply_to=5, notify=True)
    plain = await app.transport.send(CHAT, "просто так")
    reply, post = app.session.of(SendMessage)
    assert (reply.chat_id, reply.text, reply.reply_parameters.message_id) == (CHAT, "ответ", 5)
    assert reply.reply_parameters.allow_sending_without_reply  # the message may be deleted by now
    assert post.reply_parameters is None
    assert sent is not None and plain is not None and sent.message_id < plain.message_id
    assert abs(sent.created_at - time.time()) < 60


async def test_transport_reacts_and_shows_typing(app: SimpleNamespace) -> None:
    assert await app.transport.react(CHAT, 5, "🔥")
    [reaction] = app.session.of(SetMessageReaction)
    assert (reaction.chat_id, reaction.message_id, [r.emoji for r in reaction.reaction]) == (CHAT, 5, ["🔥"])

    async with app.transport.typing(CHAT), asyncio.timeout(2):
        while not app.session.of(SendChatAction):
            await asyncio.sleep(0.01)
    action = app.session.of(SendChatAction)[0]
    assert (action.chat_id, action.action) == (CHAT, "typing")


async def test_telegram_errors_are_reported_not_raised(app: SimpleNamespace) -> None:
    app.session.rejected = {SendMessage, SetMessageReaction}
    assert await app.transport.send(CHAT, "привет") is None
    assert await app.transport.react(CHAT, 5, "🔥") is False  # e.g. the chat restricts reactions
    assert not await app.responder.send(CHAT, "привет")
    assert await app.storage.count_messages(CHAT) == 0  # nothing was posted, nothing is remembered


def test_telegram_settings_are_the_core_ones_plus_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("OWNER_IDS", "1,2")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ValidationError, match="telegram_bot_token"):
        TelegramSettings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "42:TEST")
    settings = TelegramSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.telegram_bot_token.get_secret_value() == "42:TEST"
    assert settings.owner_ids == [1, 2]  # core fields and their parsing come along


def test_python_m_backseat_still_starts_the_telegram_bot() -> None:
    assert legacy_entrypoint.main is telegram_entrypoint.main
