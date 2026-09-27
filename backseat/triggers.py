"""Platform-independent parts of "is this message for the bot": its names and empty chatter."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

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
    username: str  # what follows "@" when people mention the bot
    platform: str = "Telegram"


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


def is_trivial_text(text: str) -> bool:
    """Nothing to comment on: "ок", "ахахах", "да)", emoji only."""
    words = re.findall(r"\w+|[+()\-]+", text.lower())
    if not words:
        return True  # only emoji or punctuation
    return all(word in _TRIVIAL_WORDS or _LAUGH_RE.fullmatch(word) for word in words)
