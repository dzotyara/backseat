"""What the core needs from a chat platform: post a message, react to one, show "typing…"."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Sent:
    message_id: int
    created_at: int  # unix seconds


class Transport(Protocol):
    platform: str  # "Telegram" / "Discord" — the prompts mention it
    max_length: int  # longest message the platform accepts

    async def send(
        self, chat_id: int, text: str, *, reply_to: int | None = None, notify: bool = False
    ) -> Sent | None:
        """Post a message, as a reply if reply_to is set; notify pings that message's author
        where the platform can. Returns None when the platform refused."""
        ...

    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool: ...

    def typing(self, chat_id: int) -> AbstractAsyncContextManager[object]: ...
