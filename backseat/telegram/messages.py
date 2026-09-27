"""Reading aiogram messages: what to remember about one, and whether it calls the bot."""

import re

from aiogram.types import Message

from backseat.triggers import Address, BotIdentity, is_trivial_text


def _duration(seconds: int | None) -> str:
    seconds = seconds or 0
    return f"{seconds // 60}:{seconds % 60:02d}"


def describe_media(message: Message) -> str | None:
    # Animation messages also carry `document`, so check it first.
    if message.sticker:
        return f"[стикер {message.sticker.emoji}]" if message.sticker.emoji else "[стикер]"
    if message.animation:
        return "[гифка]"
    if message.photo:
        return "[фото]"
    if message.video:
        return "[видео]"
    if message.video_note:
        return "[кружок]"
    if message.voice:
        return f"[голосовое {_duration(message.voice.duration)}]"
    if message.audio:
        title = " — ".join(part for part in (message.audio.performer, message.audio.title) if part)
        return f"[аудио {title}]" if title else "[аудио]"
    if message.document:
        name = message.document.file_name
        return f"[файл {name}]" if name else "[файл]"
    if message.poll:
        options = " / ".join(option.text for option in message.poll.options)
        return f"[опрос: {message.poll.question} — {options}]"
    if message.dice:
        return f"[{message.dice.emoji} выпало {message.dice.value}]"
    if message.location or message.venue:
        return "[геолокация]"
    if message.contact:
        return "[контакт]"
    return None


def describe_message(message: Message) -> str:
    """What we remember about a message. Empty for service messages (joins, pins, ...)."""
    media = describe_media(message)
    text = message.text or message.caption
    if not media and not text:
        return ""
    parts = ["[переслано]"] if message.forward_origin else []
    parts += [part for part in (media, text) if part]
    return " ".join(parts)


def find_address(message: Message, me: BotIdentity, names: re.Pattern[str] | None) -> Address | None:
    """How the message calls the bot, if it does. `names` comes from triggers.compile_names."""
    text = message.text or message.caption or ""
    handle = f"@{me.username}".lower() if me.username else None
    for entity in message.entities or message.caption_entities or []:
        if entity.type == "mention" and handle and entity.extract_from(text).lower() == handle:
            return Address.MENTION
        if entity.type == "text_mention" and entity.user and entity.user.id == me.id:
            return Address.MENTION
    if handle and handle in text.lower():
        return Address.MENTION
    if names and names.search(text):
        return Address.NAME
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == me.id:
        return Address.REPLY
    return None


def is_trivial(message: Message) -> bool:
    """Nothing to comment on: a bare sticker/photo, "ок", "ахахах", "да)"."""
    if message.poll:
        return False
    text = message.text or message.caption
    return not text or is_trivial_text(text)
