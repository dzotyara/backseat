"""Discord message -> what Backseat remembers about it, and whether it calls the bot."""

import re
from collections.abc import Collection, Iterable

import discord

from backseat.storage import StoredMessage
from backseat.triggers import Address, is_trivial_text


def _duration(seconds: float | None) -> str:
    seconds = round(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _describe_attachment(attachment: discord.Attachment) -> str:
    if attachment.is_voice_message():
        return f"[голосовое {_duration(attachment.duration)}]"
    kind = (attachment.content_type or "").partition("/")[0]
    if kind == "image":
        return "[картинка]"
    if kind == "video":
        return "[видео]"
    if kind == "audio":
        return "[аудио]"
    return f"[файл {attachment.filename}]"


def _media(attachments: Iterable[discord.Attachment], stickers: Iterable[discord.StickerItem]) -> list[str]:
    return [_describe_attachment(attachment) for attachment in attachments] + [
        f"[стикер {sticker.name}]" for sticker in stickers
    ]


def describe_message(message: discord.Message) -> str:
    """What we remember about a message: its text plus markers for media. Empty for system messages
    (joins, pins, boosts) and for messages with nothing to show, like a bare embed."""
    if message.is_system():
        return ""
    parts = _media(message.attachments, message.stickers)
    if message.poll is not None:
        answers = " / ".join(answer.text for answer in message.poll.answers)
        parts.append(f"[опрос: {message.poll.question} — {answers}]")
    if text := message.clean_content.strip():
        parts.append(text)
    for snapshot in message.message_snapshots:
        forwarded = _media(snapshot.attachments, snapshot.stickers)
        if snapshot_text := snapshot.content.strip():
            forwarded.append(snapshot_text)
        parts.append(" ".join(["[переслано]", *forwarded]))
    return " ".join(parts)


def find_address(message: discord.Message, me_id: int, names: re.Pattern[str] | None) -> Address | None:
    if any(user.id == me_id for user in message.mentions):
        return Address.MENTION
    # Typing "@Bot" in the client often picks the bot's own managed role instead of the bot.
    self_role = message.guild.self_role if message.guild is not None else None
    if self_role is not None and any(role.id == self_role.id for role in message.role_mentions):
        return Address.MENTION
    if names is not None and names.search(message.clean_content):
        return Address.NAME
    # A reply to a deleted message resolves to DeletedReferencedMessage, which has no author.
    reference = message.reference
    author = getattr(reference.resolved, "author", None) if reference is not None else None
    if author is not None and author.id == me_id:
        return Address.REPLY
    return None


def is_trivial(message: discord.Message) -> bool:
    """Nothing to comment on: a bare sticker or picture, "ок", "ахахах", "да)". A poll always is something."""
    if message.poll is not None:
        return False
    text = message.clean_content.strip() or " ".join(s.content for s in message.message_snapshots).strip()
    return not text or is_trivial_text(text)


def author_of(message: discord.Message) -> tuple[int, str, bool]:
    """(id, display name, is a bot). Webhook posts — feeds, integrations — count as bots."""
    author = message.author
    return author.id, author.display_name, author.bot or message.webhook_id is not None


def _reply_to(message: discord.Message) -> int | None:
    """The message this one answers; forwards and thread starters point elsewhere."""
    reference = message.reference
    if reference is None or reference.type is not discord.MessageReferenceType.reply:
        return None
    return reference.message_id if reference.channel_id == message.channel.id else None


def stored_message(message: discord.Message, me_id: int) -> StoredMessage | None:
    """The row Backseat keeps for a message, or None when there is nothing to remember."""
    text = describe_message(message)
    if not text:
        return None
    user_id, author, _ = author_of(message)
    return StoredMessage(
        chat_id=message.channel.id,
        message_id=message.id,
        user_id=user_id,
        author=author,
        text=text,
        reply_to=_reply_to(message),
        is_bot=user_id == me_id,
        created_at=int(message.created_at.timestamp()),
    )


def channel_allowed(channel: discord.abc.Snowflake, allowed_ids: Collection[int]) -> bool:
    """An empty list allows every channel. A thread is allowed when its parent channel is."""
    if not allowed_ids:
        return True
    return channel.id in allowed_ids or getattr(channel, "parent_id", None) in allowed_ids
