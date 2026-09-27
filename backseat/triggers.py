"""Is a message addressed to the bot, and is it worth an unprompted comment at all?"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from aiogram.types import Message

_VOWELS = set("аеёиоуыэюяьйaeiouy")

# fmt: off
_TRIVIAL_WORDS = {
    "ок", "окей", "окэй", "ok", "okay", "ага", "угу", "ясно", "ясн", "понял", "поняла",
    "понятно", "пон", "да", "нет", "не", "неа", "ну", "норм", "нормально", "лол", "кек",
    "спс", "спасибо", "пасиб", "мб", "хм", "хмм", "gg", "гг", "лан", "ладно", "жиза",
    "база", "ору", "го", "плюс", "+", "-", ")", "(",
}
# fmt: on
_LAUGH_RE = re.compile(r"(?:[ах]{3,}|х[аы]+|[xх]+[aа]+[xх]*|л+о+л+|к+е+к+|\++|\)+|\(+)")


@dataclass(frozen=True, slots=True)
class BotIdentity:
    id: int
    username: str


class Address(Enum):
    MENTION = "mention"  # @username or a mention picked from the member list
    NAME = "name"  # one of the bot's names in the text, e.g. "ботяра, ты тут?"
    REPLY = "reply"  # a reply to one of the bot's messages


def compile_names(names: Iterable[str]) -> re.Pattern[str] | None:
    """Whole-word regex for the bot's names that tolerates Russian case endings:
    "ботяра" also matches "ботяру"/"ботяре", "бэксит" matches "бэкситу"."""
    alternatives = []
    for raw in names:
        name = raw.strip().lower()
        if not name:
            continue
        if len(name) < 4:
            alternatives.append(re.escape(name))
            continue
        stem = name[:-1] if name[-1] in _VOWELS else name
        alternatives.append(re.escape(stem) + r"\w{0,3}")
    if not alternatives:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)", re.IGNORECASE)


def find_address(message: Message, me: BotIdentity, names: re.Pattern[str] | None) -> Address | None:
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


def is_trivial_text(text: str) -> bool:
    words = re.findall(r"\w+|[+()\-]+", text.lower())
    if not words:
        return True  # only emoji or punctuation
    return all(word in _TRIVIAL_WORDS or _LAUGH_RE.fullmatch(word) for word in words)


def is_trivial(message: Message) -> bool:
    """Nothing to comment on: a bare sticker/photo, "ок", "ахахах", "да)"."""
    if message.poll:
        return False
    text = message.text or message.caption
    return not text or is_trivial_text(text)
