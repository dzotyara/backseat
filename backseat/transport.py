"""What the core needs from a chat platform: post a message or a picture, react to one, show "typing…"."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Sent:
    message_id: int
    created_at: int  # unix seconds


def picture_filename(image: bytes) -> str:
    """A file name whose extension matches the bytes: chat apps preview a picture by its extension."""
    if image.startswith(b"\x89PNG"):
        return "picture.png"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "picture.webp"
    return "picture.jpg"


class Transport(Protocol):
    platform: str  # "Telegram" / "Discord" — the prompts mention it
    max_length: int  # longest message the platform accepts

    async def send(self, chat_id: int, text: str, *, reply_to: int | None = None, notify: bool = False) -> Sent | None:
        """Post a message, as a reply if reply_to is set; notify pings that message's author
        where the platform can. Returns None when the platform refused."""
        ...

    async def send_image(
        self, chat_id: int, image: bytes, caption: str, *, reply_to: int | None = None, notify: bool = False
    ) -> Sent | None:
        """Post a picture with a short caption, like send."""
        ...

    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool: ...

    def typing(self, chat_id: int) -> AbstractAsyncContextManager[object]: ...
