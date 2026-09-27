from types import SimpleNamespace

import discord
import pytest

from backseat.discord.transport import DiscordTransport
from backseat.transport import Sent
from tests.discord.fakes import BASE, CHANNEL, OTHER_CHANNEL, THREAD, FakeChannel, FakeClient, http_error


def only_replied_user(mentions: discord.AllowedMentions, notify: bool) -> None:
    """Model text must never ping @everyone/@here, a role or a user; at most the replied-to author."""
    assert (mentions.everyone, mentions.users, mentions.roles, mentions.replied_user) == (False, False, False, notify)
    assert mentions.to_dict()["parse"] == []


async def test_answer_pings_only_the_author_it_replies_to() -> None:
    channel = FakeChannel(CHANNEL)
    transport = DiscordTransport(FakeClient(cached=[channel]))  # type: ignore[arg-type]
    sent = await transport.send(CHANNEL, "@everyone глядите", reply_to=77, notify=True)
    [call] = channel.sent
    assert sent == Sent(call.id, int(BASE.timestamp()))
    assert call.content == "@everyone глядите"
    assert (call.reference.message_id, call.reference.channel_id) == (77, CHANNEL)
    assert call.reference.fail_if_not_exists is False  # a deleted target must not cost the answer
    only_replied_user(call.allowed_mentions, notify=True)


async def test_plain_message_pings_nobody() -> None:
    channel = FakeChannel(CHANNEL)
    transport = DiscordTransport(FakeClient(cached=[channel]))  # type: ignore[arg-type]
    assert await transport.send(CHANNEL, "просто мысль") is not None
    [call] = channel.sent
    assert call.reference is None
    only_replied_user(call.allowed_mentions, notify=False)


async def test_uncached_channel_is_fetched() -> None:
    channel = FakeChannel(THREAD)
    client = FakeClient(fetchable=[channel])
    transport = DiscordTransport(client)  # type: ignore[arg-type]
    assert await transport.send(THREAD, "в треде") is not None
    assert client.fetched == [THREAD]
    assert [call.content for call in channel.sent] == ["в треде"]


async def test_refusals_are_reported_not_raised() -> None:
    forbidden = FakeChannel(CHANNEL, error=http_error())
    category = SimpleNamespace(id=OTHER_CHANNEL)  # a category or forum id in ALLOWED_CHAT_IDS
    transport = DiscordTransport(FakeClient(cached=[forbidden, category]))  # type: ignore[arg-type]
    assert await transport.send(CHANNEL, "текст") is None
    assert await transport.react(CHANNEL, 77, "🔥") is False
    assert await transport.send(OTHER_CHANNEL, "текст") is None
    assert await transport.send(12345, "текст") is None  # the fetch answers 404
    assert await transport.react(12345, 77, "🔥") is False


async def test_react() -> None:
    channel = FakeChannel(CHANNEL)
    transport = DiscordTransport(FakeClient(cached=[channel]))  # type: ignore[arg-type]
    assert await transport.react(CHANNEL, 77, "🗿") is True
    assert channel.reactions == [(77, "🗿")]


async def test_typing_shows_while_the_body_runs() -> None:
    channel = FakeChannel(CHANNEL)
    transport = DiscordTransport(FakeClient(cached=[channel]))  # type: ignore[arg-type]
    async with transport.typing(CHANNEL):
        assert channel.typing_shown == 1
    with pytest.raises(ValueError, match="сломалось"):
        async with transport.typing(CHANNEL):
            raise ValueError("сломалось")  # the body's own errors are not swallowed


async def test_typing_failure_never_costs_the_answer() -> None:
    silent = FakeChannel(CHANNEL, typing_error=http_error())
    transport = DiscordTransport(FakeClient(cached=[silent]))  # type: ignore[arg-type]
    ran = False
    async with transport.typing(CHANNEL):
        ran = True
    async with transport.typing(12345):  # unknown channel: no indicator, still runs
        pass
    assert ran


def test_platform_limits() -> None:
    assert DiscordTransport.platform == "Discord"
    assert DiscordTransport.max_length <= 2000
