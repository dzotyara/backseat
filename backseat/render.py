"""Telegram message -> remembered text, and stored messages -> transcript lines for the model."""

from collections.abc import Iterable, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import Message

from backseat.storage import StoredMessage

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def estimate_tokens(text: str) -> int:
    """About 3 characters per token for mixed Russian/English chat — deliberately pessimistic,
    we don't have every OpenRouter model's tokenizer."""
    return len(text) // 3 + 1


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


class LineFormatter:
    """Renders "#id HH:MM Author[user_id] ↩#reply: text" lines, grouped by day."""

    def __init__(self, tz: ZoneInfo, aliases: Mapping[int, str], max_chars: int = 1500) -> None:
        self._tz = tz
        self._aliases = aliases
        self._max_chars = max_chars

    def author(self, message: StoredMessage) -> str:
        if message.is_bot:
            return "Ты"
        name = self._aliases.get(message.user_id, message.author)
        return f"{name}[{message.user_id}]"

    def line(self, message: StoredMessage) -> str:
        local = datetime.fromtimestamp(message.created_at, self._tz)
        reply = f" ↩#{message.reply_to}" if message.reply_to else ""
        text = " / ".join(part.strip() for part in message.text.splitlines() if part.strip())
        if len(text) > self._max_chars:
            text = text[: self._max_chars - 1] + "…"
        return f"#{message.message_id} {local:%H:%M} {self.author(message)}{reply}: {text}"

    def day_header(self, timestamp: int) -> str:
        local = datetime.fromtimestamp(timestamp, self._tz)
        return f"— {local:%d.%m.%Y} ({WEEKDAYS[local.weekday()]}) —"

    def lines(self, messages: Iterable[StoredMessage]) -> str:
        out: list[str] = []
        last_day = None
        for message in messages:
            day = datetime.fromtimestamp(message.created_at, self._tz).date()
            if day != last_day:
                out.append(self.day_header(message.created_at))
                last_day = day
            out.append(self.line(message))
        return "\n".join(out)

    def newest_within(self, messages: list[StoredMessage], budget: int) -> list[StoredMessage]:
        """The newest messages whose lines fit into `budget` tokens, oldest first."""
        picked: list[StoredMessage] = []
        used = 0
        for message in reversed(messages):
            cost = estimate_tokens(self.line(message))
            if used + cost > budget:
                break
            picked.append(message)
            used += cost
        picked.reverse()
        return picked
