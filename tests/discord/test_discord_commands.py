import time
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from backseat.discord.commands import (
    NOT_OWNER,
    Services,
    application_owners,
    cmd_digest,
    cmd_help,
    cmd_names,
    cmd_prompt,
    cmd_status,
    register_commands,
)
from backseat.discord.settings import DiscordSettings
from backseat.llm import LLMError
from backseat.storage import Storage
from backseat.transport import Sent
from tests.discord.fakes import (
    BASE,
    CHANNEL,
    OTHER_CHANNEL,
    OWNER,
    PETYA,
    THREAD,
    FakeDigest,
    FakeInteraction,
    make_services,
    text_channel,
    thread,
)


class TextFile:
    """A discord.Attachment as far as /prompt is concerned."""

    def __init__(self, filename: str, data: bytes, content_type: str | None = None) -> None:
        self.filename = filename
        self.content_type = content_type
        self.size = len(data)
        self._data = data

    async def read(self) -> bytes:
        return self._data


@pytest.fixture
def svc(settings: DiscordSettings, storage: Storage) -> Services:
    return make_services(settings, storage)


def only_reply(interaction: FakeInteraction) -> SimpleNamespace:
    [reply] = interaction.replies()
    assert reply.ephemeral, "must be visible to the caller alone"
    return reply


async def test_help_lists_owner_commands_to_owners_only(svc: Services) -> None:
    member, owner = FakeInteraction(PETYA), FakeInteraction(OWNER)
    await cmd_help(svc, member)  # type: ignore[arg-type]
    await cmd_help(svc, owner)  # type: ignore[arg-type]
    text = only_reply(member).content
    assert "@Бэксит" in text and "бэксит, ботяра" in text and "/digest" in text
    assert "/prompt" not in text and "/status" not in text
    assert "/prompt" in only_reply(owner).content


async def test_names_are_shown_to_everyone_but_changed_by_the_owner_only(svc: Services) -> None:
    look = FakeInteraction(PETYA)
    await cmd_names(svc, look, None)  # type: ignore[arg-type]
    assert only_reply(look).content == "Откликаюсь на: бэксит, ботяра и @Бэксит"

    denied = FakeInteraction(PETYA)
    await cmd_names(svc, denied, "петька")  # type: ignore[arg-type]
    assert "только владелец" in only_reply(denied).content
    assert await svc.bot_config.names() == ["бэксит", "ботяра"]

    change = FakeInteraction(OWNER)
    await cmd_names(svc, change, "Бэкс, ботяра; бэкс, ботяра")  # type: ignore[arg-type]
    assert await svc.bot_config.names() == ["Бэкс", "ботяра", "бэкс"]
    assert only_reply(change).content.startswith("Теперь откликаюсь на: Бэкс, ботяра, бэкс")

    reset = FakeInteraction(OWNER)
    await cmd_names(svc, reset, "Сброс")  # type: ignore[arg-type]
    assert await svc.bot_config.names() == ["бэксит", "ботяра"]
    assert "по умолчанию" in only_reply(reset).content


async def test_status_is_for_the_owner_only(svc: Services) -> None:
    denied = FakeInteraction(PETYA)
    await cmd_status(svc, denied)  # type: ignore[arg-type]
    assert only_reply(denied).content == NOT_OWNER
    assert denied.response.deferred is None


async def test_status_report(svc: Services) -> None:
    await svc.storage.set_summary(CHANNEL, "сводка", upto_message_id=1, updated_at=int(BASE.timestamp()))
    interaction = FakeInteraction(OWNER)
    await cmd_status(svc, interaction)  # type: ignore[arg-type]
    assert interaction.response.deferred == SimpleNamespace(ephemeral=True, thinking=True)
    text = only_reply(interaction).content
    assert "Модели по порядку: paid/model → free/model:free" in text
    assert "Последний ответ дала: paid/model" in text
    assert f"Память <#{CHANNEL}>: 0 сообщений, сводка обновлена 20.09 15:00" in text  # TIMEZONE is Moscow
    assert "Имена: бэксит, ботяра" in text
    assert "Бесплатные запросы сегодня: 3 из 50" in text
    assert "Потрачено сегодня: $0.0123" in text


async def test_prompt_is_for_the_owner_only(svc: Services) -> None:
    for arguments in ({}, {"text": "Ты злой бот."}, {"reset": True}):
        interaction = FakeInteraction(PETYA)
        await cmd_prompt(svc, interaction, **arguments)  # type: ignore[arg-type]
        assert only_reply(interaction).content == NOT_OWNER
    assert not await svc.bot_config.has_custom_persona()


async def test_prompt_shows_the_persona_privately(svc: Services) -> None:
    short = FakeInteraction(OWNER)
    await cmd_prompt(svc, short)  # type: ignore[arg-type]
    assert only_reply(short).content == "Характер по умолчанию:\n\nТы — тестовый бот."

    long_persona = "Стеби Ивана. " * 200
    await svc.bot_config.set_persona(long_persona)
    long = FakeInteraction(OWNER)
    await cmd_prompt(svc, long)  # type: ignore[arg-type]
    reply = only_reply(long)
    assert isinstance(reply.file, discord.File) and reply.file.filename == "prompt.txt"
    assert reply.file.fp.read().decode("utf-8") == long_persona


async def test_prompt_set_by_text_or_file_and_reset(svc: Services) -> None:
    by_text = FakeInteraction(OWNER)
    await cmd_prompt(svc, by_text, text="  Ты — ворчливый дед.  ")  # type: ignore[arg-type]
    assert await svc.bot_config.persona() == "Ты — ворчливый дед."
    assert "сохранён" in only_reply(by_text).content

    by_file = FakeInteraction(OWNER)
    persona = TextFile("persona.md", "﻿Ты — поэт.".encode())
    await cmd_prompt(svc, by_file, file=persona)  # type: ignore[arg-type]
    assert await svc.bot_config.persona() == "Ты — поэт."

    legacy = FakeInteraction(OWNER)
    await cmd_prompt(svc, legacy, file=TextFile("persona.txt", "Ты — бард.".encode("cp1251")))  # type: ignore[arg-type]
    assert await svc.bot_config.persona() == "Ты — бард."

    for rejected in (TextFile("cat.png", b"\x89PNG", "image/png"), TextFile("big.txt", b"x" * 100_001)):
        interaction = FakeInteraction(OWNER)
        await cmd_prompt(svc, interaction, file=rejected)  # type: ignore[arg-type]
        assert "Не смог прочитать файл" in only_reply(interaction).content
    assert await svc.bot_config.persona() == "Ты — бард."

    for arguments in ({"reset": True}, {"text": "сброс"}):
        await svc.bot_config.set_persona("Ты — бард.")
        reset = FakeInteraction(OWNER)
        await cmd_prompt(svc, reset, **arguments)  # type: ignore[arg-type]
        assert not await svc.bot_config.has_custom_persona()
        assert only_reply(reset).content == "Вернул характер по умолчанию."


async def test_digest_is_posted_publicly_split_and_remembered(svc: Services) -> None:
    lines = ["Итоги недели"] + [f"— пункт {i}: " + ", ".join(["очень важное событие"] * 5) for i in range(25)]
    svc.digest.results.append("\n".join(lines))  # type: ignore[attr-defined]
    interaction = FakeInteraction(PETYA)
    await cmd_digest(svc, interaction)  # type: ignore[arg-type]

    assert interaction.response.deferred == SimpleNamespace(ephemeral=False, thinking=True)
    [(chat_id, since)] = svc.digest.calls  # type: ignore[attr-defined]
    assert chat_id == CHANNEL
    assert abs(since - (time.time() - 7 * 24 * 3600)) < 60
    posts = interaction.followup.sent
    assert len(posts) == 2 and all(len(post.content) <= 1900 for post in posts)
    assert "\n".join(post.content for post in posts) == "\n".join(lines)
    for post in posts:
        assert not post.ephemeral
        assert post.allowed_mentions.to_dict() == {"parse": []}  # model text pings nobody
    assert svc.responder.remembered == [  # type: ignore[attr-defined]
        (CHANNEL, Sent(post.id, int(BASE.timestamp())), post.content) for post in posts
    ]


async def test_digest_cooldown_per_channel(svc: Services) -> None:
    svc.digest.results.extend(["Итоги недели", "Итоги треда", "Итоги недели снова"])  # type: ignore[attr-defined]
    await cmd_digest(svc, FakeInteraction(PETYA))  # type: ignore[arg-type]

    again = FakeInteraction(OWNER)
    await cmd_digest(svc, again)  # type: ignore[arg-type]
    assert only_reply(again).content == "Итоги недавно подводили, подожди 10 мин."
    svc.clock.now += 590  # type: ignore[attr-defined]
    soon = FakeInteraction(PETYA)
    await cmd_digest(svc, soon)  # type: ignore[arg-type]
    assert only_reply(soon).content.endswith("подожди 1 мин.")

    in_thread = FakeInteraction(PETYA, thread(THREAD, parent_id=CHANNEL))  # a thread is a channel of its own
    await cmd_digest(svc, in_thread)  # type: ignore[arg-type]
    assert [post.content for post in in_thread.followup.sent] == ["Итоги треда"]

    svc.clock.now += 11  # type: ignore[attr-defined]
    later = FakeInteraction(PETYA)
    await cmd_digest(svc, later)  # type: ignore[arg-type]
    assert [post.content for post in later.followup.sent] == ["Итоги недели снова"]
    assert [call[0] for call in svc.digest.calls] == [CHANNEL, THREAD, CHANNEL]  # type: ignore[attr-defined]


async def test_digest_only_in_allowed_channels(svc: Services) -> None:
    interaction = FakeInteraction(PETYA, text_channel(OTHER_CHANNEL))
    await cmd_digest(svc, interaction)  # type: ignore[arg-type]
    assert only_reply(interaction).content == "Здесь я итоги не подвожу."
    assert interaction.response.deferred is None
    assert svc.digest.calls == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("result", "apology"),
    [(LLMError("все модели упали"), "Не получилось подвести итоги"), (None, "Подводить нечего")],
)
async def test_digest_failure_is_told_privately_and_can_be_retried(
    settings: DiscordSettings, storage: Storage, result: Exception | None, apology: str
) -> None:
    svc = make_services(settings, storage, digest=FakeDigest(result, "Итоги недели"))
    failed = FakeInteraction(PETYA)
    await cmd_digest(svc, failed)  # type: ignore[arg-type]
    assert failed.original_deleted, "the public «thinking…» message must go away"
    [reply] = failed.followup.sent
    assert reply.ephemeral and reply.content.startswith(apology)
    assert svc.responder.remembered == []  # type: ignore[attr-defined]

    retry = FakeInteraction(PETYA)  # no cooldown: nothing was posted
    await cmd_digest(svc, retry)  # type: ignore[arg-type]
    assert [post.content for post in retry.followup.sent] == ["Итоги недели"]


def test_application_owners() -> None:
    solo = SimpleNamespace(team=None, owner=SimpleNamespace(id=OWNER))
    team = SimpleNamespace(team=SimpleNamespace(members=[SimpleNamespace(id=1), SimpleNamespace(id=2)]), owner=None)
    assert application_owners(solo) == {OWNER}  # type: ignore[arg-type]
    assert application_owners(team) == {1, 2}  # type: ignore[arg-type]


async def test_commands_are_guild_only_and_owner_ones_hidden(svc: Services) -> None:
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.default()))
    register_commands(tree, svc)
    payloads = {command.name: command.to_dict(tree) for command in tree.get_commands()}
    assert set(payloads) == {"help", "names", "digest", "status", "prompt"}
    for name, payload in payloads.items():
        assert payload["contexts"] == [0], f"/{name} must be guild-only"
        assert len(payload["description"]) <= 100
        owner_only = name in {"status", "prompt"}
        # Administrator only (bit 3): regular members don't even see the owner commands.
        assert payload["default_member_permissions"] == (8 if owner_only else None)
    assert [option["name"] for option in payloads["prompt"]["options"]] == ["text", "file", "reset"]
    assert [option["type"] for option in payloads["prompt"]["options"]] == [3, 11, 5]  # string, attachment, bool
    assert [option["required"] for option in payloads["names"]["options"]] == [False]
