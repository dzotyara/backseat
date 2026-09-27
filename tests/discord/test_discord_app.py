"""BackseatClient's event handlers, driven directly: routing, edits, the catch-up and the wiring."""

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backseat.discord.app import BackseatClient
from backseat.discord.settings import DiscordSettings
from backseat.responder import Incoming, Responder
from backseat.storage import Storage, StoredMessage
from backseat.triggers import BotIdentity
from tests.discord.fakes import (
    BASE,
    BOT_ID,
    CHANNEL,
    OTHER_BOT,
    OTHER_CHANNEL,
    OWNER,
    PETYA,
    THREAD,
    FakeChannel,
    FakeClient,
    FakeDigest,
    FakeLLM,
    FakeResponder,
    FakeSummarizer,
    http_error,
    make_message,
    make_reference,
    make_services,
    make_settings,
    sticker,
    text_channel,
    thread,
)

APP_OWNER = 300000000000000009


@pytest.fixture
def client(settings: DiscordSettings, storage: Storage) -> BackseatClient:
    client = BackseatClient(settings, storage, FakeLLM())  # type: ignore[arg-type]
    client.services = make_services(settings, storage, bot_config=client.bot_config)
    return client


def queued(client: BackseatClient) -> list[tuple[int, Any]]:
    assert client.services is not None
    responder = client.services.responder
    assert isinstance(responder, FakeResponder)
    return responder.enqueued


async def test_channel_message_is_stored_and_queued(client: BackseatClient, storage: Storage) -> None:
    message = make_message("кто сегодня в бар?")
    await client.on_message(message)
    stored = await storage.get_message(CHANNEL, message.id)
    assert stored is not None
    assert (stored.text, stored.user_id, stored.author, stored.is_bot) == ("кто сегодня в бар?", PETYA, "Петя", False)
    assert queued(client) == [(CHANNEL, Incoming(message.id, PETYA, addressed=False, trivial=False))]


async def test_every_call_is_answered(client: BackseatClient) -> None:
    await client.on_message(make_message("@Бэксит что думаешь?", mentions=[BOT_ID]))
    await client.on_message(make_message("бэксит, скажи"))
    await client.on_message(make_message(stickers=[sticker("Wave")], reference=make_reference(5, author_id=BOT_ID)))
    await client.on_message(make_message("ок"))
    flags = [(incoming.addressed, incoming.trivial) for _, incoming in queued(client)]
    assert flags == [(True, False), (True, False), (True, True), (False, True)]


async def test_a_thread_of_an_allowed_channel_is_a_chat_of_its_own(client: BackseatClient, storage: Storage) -> None:
    message = make_message("в треде", channel=thread(THREAD, parent_id=CHANNEL))
    await client.on_message(message)
    assert await storage.get_message(THREAD, message.id) is not None
    assert [chat_id for chat_id, _ in queued(client)] == [THREAD]


async def test_ignored_messages(client: BackseatClient, storage: Storage) -> None:
    for message in (
        make_message("чужой канал", channel=text_channel(OTHER_CHANNEL)),
        make_message("чужой тред", channel=thread(THREAD, parent_id=OTHER_CHANNEL)),
        make_message("в личку", dm=True),
        make_message("я сам", author_id=BOT_ID, author_name="Бэксит", bot=True),  # stored when it was sent
        make_message("Петя закрепил сообщение", system=True),
        make_message(""),  # a bare embed
    ):
        await client.on_message(message)
    assert queued(client) == []
    for chat_id in (CHANNEL, THREAD, OTHER_CHANNEL):
        assert await storage.count_messages(chat_id) == 0


async def test_other_bots_are_remembered_but_never_answered(client: BackseatClient, storage: Storage) -> None:
    message = make_message("@Бэксит новый трек", author_id=OTHER_BOT, author_name="Музыка", bot=True, mentions=[BOT_ID])
    await client.on_message(message)
    assert await storage.get_message(CHANNEL, message.id) is not None
    assert queued(client) == []


async def test_nothing_happens_before_setup(settings: DiscordSettings, storage: Storage) -> None:
    client = BackseatClient(settings, storage, FakeLLM())  # type: ignore[arg-type]
    await client.on_message(make_message("рано"))
    assert await storage.count_messages(CHANNEL) == 0


async def test_edits_update_the_stored_text(client: BackseatClient, storage: Storage) -> None:
    message = make_message("превед")
    await client.on_message(message)
    edited = make_message("привет", message_id=message.id, edited_at=BASE + timedelta(minutes=1))
    await client.on_raw_message_edit(SimpleNamespace(message=edited))  # type: ignore[arg-type]
    # A link preview arriving later is an update without an edit time.
    await client.on_raw_message_edit(SimpleNamespace(message=make_message("превед", message_id=message.id)))  # type: ignore[arg-type]
    stored = await storage.get_message(CHANNEL, message.id)
    assert stored is not None and stored.text == "привет"

    # Channels the bot does not live in stay untouched.
    await storage.add_message(dataclasses.replace(stored, chat_id=OTHER_CHANNEL))
    elsewhere = make_message("чужая правка", message_id=message.id, channel=text_channel(OTHER_CHANNEL), edited_at=BASE)
    await client.on_raw_message_edit(SimpleNamespace(message=elsewhere))  # type: ignore[arg-type]
    other = await storage.get_message(OTHER_CHANNEL, message.id)
    assert other is not None and other.text == "привет"


async def test_catch_up_backfills_and_folds_each_allowed_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first channel refuses to show its history; that must not stop the second one.
    settings = make_settings(tmp_path, allowed_chat_ids=[OTHER_CHANNEL, CHANNEL])
    storage = Storage(settings.db_path)
    await storage.connect()
    client = BackseatClient(settings, storage, FakeLLM())  # type: ignore[arg-type]
    channels = FakeClient(
        cached=[FakeChannel(OTHER_CHANNEL, error=http_error())],
        fetchable=[FakeChannel(CHANNEL, history=[make_message("давнее"), make_message("совсем давнее")])],
    )
    monkeypatch.setattr(client, "get_channel", channels.get_channel)
    monkeypatch.setattr(client, "fetch_channel", channels.fetch_channel)
    summarizer = FakeSummarizer()  # the two messages fit the verbatim window: nothing to fold
    client.summarizer = summarizer  # type: ignore[assignment]
    try:
        await client.catch_up(BOT_ID)
        assert await storage.count_messages(CHANNEL) == 2
        assert await storage.get_meta(f"backfill:{CHANNEL}")
        assert await storage.get_meta(f"backfill:{OTHER_CHANNEL}") is None  # tried again on the next start
        assert summarizer.calls == [CHANNEL]
    finally:
        await storage.close()


async def test_no_per_batch_maintenance_while_the_catch_up_folds(
    client: BackseatClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "get_channel", FakeClient(cached=[FakeChannel(CHANNEL)]).get_channel)

    class FoldingSummarizer(FakeSummarizer):
        async def update_once(self, chat_id: int) -> bool:
            # Batches finish in both channels while the history of CHANNEL is being folded.
            await client.after_batch(CHANNEL)
            await client.after_batch(OTHER_CHANNEL)
            return await super().update_once(chat_id)

    summarizer = FoldingSummarizer()
    client.summarizer = summarizer  # type: ignore[assignment]
    await client.catch_up(BOT_ID)
    assert summarizer.maintained == [OTHER_CHANNEL]
    await client.after_batch(CHANNEL)  # the catch-up is over
    assert summarizer.maintained == [OTHER_CHANNEL, CHANNEL]


async def test_on_ready_starts_the_background_jobs_once(
    tmp_path: Path, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, weekly_digest=True)
    client = BackseatClient(settings, storage, FakeLLM())  # type: ignore[arg-type]
    digest = FakeDigest()
    client.services = make_services(settings, storage, digest=digest)
    caught_up: list[int] = []

    async def catch_up(me_id: int) -> None:
        caught_up.append(me_id)

    monkeypatch.setattr(client, "catch_up", catch_up)
    await client.on_ready()
    await client.on_ready()  # the gateway had to start a new session
    await asyncio.sleep(0)
    assert caught_up == [BOT_ID]
    assert digest.schedules_started == 1
    await client.shutdown()


@pytest.fixture
async def logged_in(
    settings: DiscordSettings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[BackseatClient]:
    """A client as setup_hook finds it: logged in, application info fetched, not connected."""
    user = SimpleNamespace(id=BOT_ID, display_name="Бэксит")
    application = SimpleNamespace(team=None, owner=SimpleNamespace(id=APP_OWNER))
    monkeypatch.setattr(BackseatClient, "user", property(lambda self: user))
    monkeypatch.setattr(BackseatClient, "application", property(lambda self: application))
    client = BackseatClient(settings, storage, FakeLLM())  # type: ignore[arg-type]
    yield client
    await client.shutdown()


async def set_up(client: BackseatClient, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Run setup_hook; returns the arguments of every slash command sync."""
    syncs: list[dict[str, Any]] = []

    async def sync(**kwargs: Any) -> list[Any]:
        syncs.append(kwargs)
        return []

    monkeypatch.setattr(client.tree, "sync", sync)
    await client.setup_hook()
    return syncs


async def test_setup_wires_the_bot_before_any_event(logged_in: BackseatClient, monkeypatch: pytest.MonkeyPatch) -> None:
    syncs = await set_up(logged_in, monkeypatch)
    svc = logged_in.services
    assert svc is not None
    assert svc.me == BotIdentity(id=BOT_ID, username="Бэксит")
    assert svc.bot_config.platform == "Discord"
    assert svc.owners == {OWNER, APP_OWNER}
    assert isinstance(svc.responder, Responder)
    commands = {command.name for command in logged_in.tree.get_commands()}
    assert commands == {"help", "names", "digest", "status", "prompt"}
    assert syncs == [{}]  # synced globally, not to one guild


async def test_a_mention_gets_a_reply_that_pings_only_its_author(
    logged_in: BackseatClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real Responder and transport end to end: stored, answered in a reply, remembered."""
    channel = FakeChannel(CHANNEL)
    monkeypatch.setattr(logged_in, "get_channel", FakeClient(cached=[channel]).get_channel)
    await set_up(logged_in, monkeypatch)
    logged_in.llm.answers.append("Нормально, сам как?")  # type: ignore[attr-defined]
    assert logged_in.services is not None

    question = make_message("@Бэксит как дела?", mentions=[BOT_ID])
    await logged_in.on_message(question)
    await logged_in.services.responder.process(CHANNEL)  # no need to wait for the debounce timer

    [reply] = channel.sent
    assert reply.content == "Нормально, сам как?"
    assert reply.reference.message_id == question.id
    mentions = reply.allowed_mentions
    assert (mentions.everyone, mentions.users, mentions.roles, mentions.replied_user) == (False, False, False, True)
    assert channel.typing_shown == 1
    remembered = await storage.get_message(CHANNEL, reply.id)
    assert remembered is not None and remembered.is_bot and remembered.reply_to == question.id


async def test_the_summary_prompt_names_discord(tmp_path: Path) -> None:
    class RecordingLLM(FakeLLM):
        def __init__(self) -> None:
            super().__init__("Сводка")
            self.prompts: list[list[dict[str, str]]] = []

        async def complete(self, messages: list[dict[str, str]], **options: Any) -> Any:
            self.prompts.append(messages)
            return await super().complete(messages, **options)

    settings = make_settings(tmp_path, recent_context_tokens=50, summary_chunk_tokens=200)
    storage = Storage(settings.db_path)
    await storage.connect()
    try:
        await storage.add_messages(
            [
                StoredMessage(
                    CHANNEL,
                    1_300_000_000_000_000_000 + i,
                    PETYA,
                    "Петя",
                    f"сообщение номер {i}",
                    None,
                    False,
                    1_790_000_000 + i,
                )
                for i in range(1, 30)
            ]
        )
        llm = RecordingLLM()
        client = BackseatClient(settings, storage, llm)  # type: ignore[arg-type]
        assert await client.summarizer.update_once(CHANNEL)
        assert "группового чата в Discord" in llm.prompts[0][0]["content"]
    finally:
        await storage.close()
