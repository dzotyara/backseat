"""The core's Transport over the Telegram Bot API."""

import logging
from contextlib import AbstractAsyncContextManager

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, ReactionTypeEmoji, ReplyParameters
from aiogram.utils.chat_action import ChatActionSender

from backseat.transport import Sent, picture_filename

log = logging.getLogger(__name__)


class TelegramTransport:
    platform = "Telegram"
    max_length = 4000  # a bit under Telegram's 4096 to leave room for surrogate pairs

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, chat_id: int, text: str, *, reply_to: int | None = None, notify: bool = False) -> Sent | None:
        # `notify` needs nothing here: a Telegram reply already notifies the author.
        reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
        try:
            sent = await self._bot.send_message(chat_id, text, reply_parameters=reply)
        except TelegramAPIError:
            log.exception("chat=%s failed to send a message", chat_id)
            return None
        return Sent(sent.message_id, int(sent.date.timestamp()))

    async def send_image(
        self, chat_id: int, image: bytes, caption: str, *, reply_to: int | None = None, notify: bool = False
    ) -> Sent | None:
        reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
        photo = BufferedInputFile(image, filename=picture_filename(image))
        try:
            sent = await self._bot.send_photo(chat_id, photo, caption=caption[:1024] or None, reply_parameters=reply)
        except TelegramAPIError:
            log.exception("chat=%s failed to send a picture", chat_id)
            return None
        return Sent(sent.message_id, int(sent.date.timestamp()))

    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool:
        try:
            await self._bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji=emoji)]
            )
        except TelegramAPIError as exc:
            # The chat may restrict reactions; that's fine, just stay quiet.
            log.info("chat=%s reaction %s rejected: %s", chat_id, emoji, exc)
            return False
        return True

    def typing(self, chat_id: int) -> AbstractAsyncContextManager[object]:
        return ChatActionSender.typing(bot=self._bot, chat_id=chat_id)
