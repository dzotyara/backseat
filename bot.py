import asyncio
import logging
import os
import time
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.types import Message

from llm_client import decide_and_comment

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backseat")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_PROMPT = os.environ.get(
    "BOT_PROMPT", "Комментируй сообщения участников чата коротко и едко."
)
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "15"))
# Safety net so a chatty LLM can't spam the chat even if it decides "yes" repeatedly.
MIN_SECONDS_BETWEEN_COMMENTS = float(os.environ.get("MIN_SECONDS_BETWEEN_COMMENTS", "20"))

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# per-chat message history buffer, used as context for the model's decision
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=CONTEXT_WINDOW))
last_comment_at: dict[int, float] = defaultdict(float)


def format_history(chat_id: int) -> str:
    return "\n".join(f"{name}: {text}" for name, text in history[chat_id])


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def on_group_message(message: Message):
    chat_id = message.chat.id
    name = message.from_user.full_name if message.from_user else "unknown"
    history[chat_id].append((name, message.text))

    now = time.monotonic()
    if now - last_comment_at[chat_id] < MIN_SECONDS_BETWEEN_COMMENTS:
        return

    bot_username = (await bot.get_me()).username
    mentioned = bool(bot_username) and f"@{bot_username}".lower() in message.text.lower()
    is_reply_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.username == bot_username
    )
    forced = mentioned or is_reply_to_bot

    context_text = format_history(chat_id)

    try:
        comment = await decide_and_comment(BOT_PROMPT, context_text, forced=forced)
    except Exception:
        log.exception("Failed to get a decision from the LLM")
        return

    if not comment:
        return

    last_comment_at[chat_id] = now
    await message.reply(comment)


async def main():
    log.info("Starting Backseat bot")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
