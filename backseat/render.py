"""How stored messages look to the model: "#n HH:MM Author[id] ↩#m: text" lines grouped by day."""

from collections.abc import Iterable, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from backseat.storage import StoredMessage

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def estimate_tokens(text: str) -> int:
    """About 3 characters per token for mixed Russian/English chat — deliberately pessimistic,
    we don't have every OpenRouter model's tokenizer."""
    return len(text) // 3 + 1


class IdMap:
    """Short numbers for the messages shown in one prompt, in the order given (duplicates keep their
    first number). Discord ids are 19-digit snowflakes: they waste tokens, and the model has to copy
    one back to reply."""

    def __init__(self, message_ids: Iterable[int]) -> None:
        self._short = {real: short for short, real in enumerate(dict.fromkeys(message_ids), start=1)}
        self._real = {short: real for real, short in self._short.items()}

    def short(self, message_id: int) -> int | None:
        return self._short.get(message_id)

    def real(self, short: int | None) -> int | None:
        return None if short is None else self._real.get(short)


class LineFormatter:
    def __init__(self, tz: ZoneInfo, aliases: Mapping[int, str], max_chars: int = 1500) -> None:
        self._tz = tz
        self._aliases = aliases
        self._max_chars = max_chars

    def author(self, message: StoredMessage) -> str:
        if message.is_bot:
            return "Ты"
        name = self._aliases.get(message.user_id, message.author)
        return f"{name}[{message.user_id}]"

    def _text(self, message: StoredMessage) -> str:
        text = " / ".join(part.strip() for part in message.text.splitlines() if part.strip())
        return text if len(text) <= self._max_chars else text[: self._max_chars - 1] + "…"

    def line(self, message: StoredMessage, ids: IdMap) -> str:
        local = datetime.fromtimestamp(message.created_at, self._tz)
        reply = ""
        if message.reply_to is not None:
            target = ids.short(message.reply_to)
            reply = f" ↩#{target}" if target else " ↩"  # a reply to something not shown
        return f"#{ids.short(message.message_id)} {local:%H:%M} {self.author(message)}{reply}: {self._text(message)}"

    def cost(self, message: StoredMessage) -> int:
        """Token estimate of a line, without needing its number yet."""
        return estimate_tokens(f"#000 00:00 {self.author(message)} ↩#000: {self._text(message)}")

    def day_header(self, timestamp: int) -> str:
        local = datetime.fromtimestamp(timestamp, self._tz)
        return f"— {local:%d.%m.%Y} ({WEEKDAYS[local.weekday()]}) —"

    def lines(self, messages: Iterable[StoredMessage], ids: IdMap) -> str:
        out: list[str] = []
        last_day = None
        for message in messages:
            day = datetime.fromtimestamp(message.created_at, self._tz).date()
            if day != last_day:
                out.append(self.day_header(message.created_at))
                last_day = day
            out.append(self.line(message, ids))
        return "\n".join(out)

    def newest_within(self, messages: list[StoredMessage], budget: int) -> list[StoredMessage]:
        """The newest messages whose lines fit into `budget` tokens, oldest first."""
        picked: list[StoredMessage] = []
        used = 0
        for message in reversed(messages):
            cost = self.cost(message)
            if used + cost > budget:
                break
            picked.append(message)
            used += cost
        picked.reverse()
        return picked
