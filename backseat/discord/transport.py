"""Transport over discord.py. Model text never pings anyone: no @everyone/@here, roles or users —
only the author of the message being answered, and only when the core asks for it."""

import contextlib
import io
import logging
from collections.abc import AsyncIterator

import discord

from backseat.transport import Sent, picture_filename

log = logging.getLogger(__name__)

# The core's reaction list uses Telegram's spelling; Discord knows the heart only with the
# emoji variation selector and rejects the bare "❤" as an unknown emoji.
_DISCORD_SPELLING = {"❤": "❤️"}


class DiscordTransport:
    platform = "Discord"
    max_length = 1900  # Discord allows 2000 characters

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def channel(self, chat_id: int) -> discord.abc.Messageable | None:
        """A channel or thread from the cache, fetched from the API if it is not cached."""
        channel = self._client.get_channel(chat_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(chat_id)
            except (discord.HTTPException, discord.InvalidData) as exc:
                log.warning("channel=%s is unavailable: %s", chat_id, exc)
                return None
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("channel=%s is a %s, not a text channel", chat_id, type(channel).__name__)
            return None
        return channel

    async def send(self, chat_id: int, text: str, *, reply_to: int | None = None, notify: bool = False) -> Sent | None:
        channel = await self.channel(chat_id)
        if channel is None:
            return None
        reference = None
        if reply_to is not None:
            # A deleted target is no reason to lose the answer: then it goes out as a plain message.
            reference = discord.MessageReference(message_id=reply_to, channel_id=chat_id, fail_if_not_exists=False)
        mentions = discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=notify)
        try:
            message = await channel.send(text, reference=reference, allowed_mentions=mentions)
        except discord.HTTPException as exc:
            log.warning("channel=%s send failed: %s", chat_id, exc)
            return None
        return Sent(message.id, int(message.created_at.timestamp()))

    async def send_image(
        self, chat_id: int, image: bytes, caption: str, *, reply_to: int | None = None, notify: bool = False
    ) -> Sent | None:
        channel = await self.channel(chat_id)
        if channel is None:
            return None
        reference = None
        if reply_to is not None:
            reference = discord.MessageReference(message_id=reply_to, channel_id=chat_id, fail_if_not_exists=False)
        mentions = discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=notify)
        picture = discord.File(io.BytesIO(image), filename=picture_filename(image))
        try:
            message = await channel.send(
                caption[: self.max_length] or None, file=picture, reference=reference, allowed_mentions=mentions
            )
        except discord.HTTPException as exc:
            log.warning("channel=%s picture send failed: %s", chat_id, exc)
            return None
        return Sent(message.id, int(message.created_at.timestamp()))

    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool:
        channel = await self.channel(chat_id)
        if channel is None:
            return False
        emoji = _DISCORD_SPELLING.get(emoji, emoji)
        try:
            await channel.get_partial_message(message_id).add_reaction(emoji)  # type: ignore[attr-defined]
        except discord.HTTPException as exc:
            log.warning("channel=%s reaction %s to message=%s failed: %s", chat_id, emoji, message_id, exc)
            return False
        return True

    @contextlib.asynccontextmanager
    async def typing(self, chat_id: int) -> AsyncIterator[None]:
        """Shows "…is typing" while the body runs; failing to show it never costs the answer."""
        async with contextlib.AsyncExitStack() as stack:
            channel = await self.channel(chat_id)
            if channel is not None:
                try:
                    await stack.enter_async_context(channel.typing())
                except discord.HTTPException as exc:
                    log.warning("channel=%s typing indicator failed: %s", chat_id, exc)
            yield
