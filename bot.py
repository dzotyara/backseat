import asyncio
import logging
import os
import random
import time
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message

from llm_client import decide_and_comment, estimate_tokens

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backseat")


def _env(name: str, default: str) -> str:
    """Like os.environ.get, but treats an empty string as 'not set' too
    (env_file entries like FOO= otherwise override real defaults with '')."""
    value = os.environ.get(name)
    return value if value else default


TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_PROMPT = _env("BOT_PROMPT", "Комментируй сообщения участников чата коротко и едко.")
CONTEXT_WINDOW = int(_env("CONTEXT_WINDOW", "15"))
MAX_INPUT_TOKENS = int(_env("MAX_INPUT_TOKENS", "1500"))
# Comment roughly once every MIN..MAX messages (random each time), unless mentioned/replied-to/asked a question.
MIN_MESSAGES_BETWEEN_COMMENTS = int(_env("MIN_MESSAGES_BETWEEN_COMMENTS", "2"))
MAX_MESSAGES_BETWEEN_COMMENTS = int(_env("MAX_MESSAGES_BETWEEN_COMMENTS", "15"))
# Safety net so the bot can't reply more than once per this many seconds even if triggered repeatedly.
MIN_SECONDS_BETWEEN_COMMENTS = float(_env("MIN_SECONDS_BETWEEN_COMMENTS", "10"))

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# per-chat message history buffer, used as context for the model's decision
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=CONTEXT_WINDOW))
messages_since_check: dict[int, int] = defaultdict(int)
next_threshold: dict[int, int] = {}
last_comment_at: dict[int, float] = defaultdict(float)

BOT_COMMANDS = [
    BotCommand(command="start", description="О боте и как он работает"),
    BotCommand(command="help", description="Список команд"),
    BotCommand(command="ping", description="Проверка, что бот жив"),
    BotCommand(command="status", description="Текущие настройки бота"),
]


def get_threshold(chat_id: int) -> int:
    if chat_id not in next_threshold:
        next_threshold[chat_id] = random.randint(
            MIN_MESSAGES_BETWEEN_COMMENTS, MAX_MESSAGES_BETWEEN_COMMENTS
        )
    return next_threshold[chat_id]


def reset_threshold(chat_id: int) -> None:
    """Call after every actual LLM check (whether or not it produced a comment)
    to restart the countdown to the next scheduled check."""
    next_threshold[chat_id] = random.randint(
        MIN_MESSAGES_BETWEEN_COMMENTS, MAX_MESSAGES_BETWEEN_COMMENTS
    )
    messages_since_check[chat_id] = 0


def format_history(chat_id: int) -> str:
    lines = [f"{name}: {text}" for name, text in history[chat_id]]
    while lines and estimate_tokens("\n".join(lines)) > MAX_INPUT_TOKENS:
        lines.pop(0)
    return "\n".join(lines)


def looks_like_question(text: str) -> bool:
    return text.strip().endswith("?")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.reply(
        "Привет, я Backseat. Иногда встреваю в чат с комментариями по своему характеру, "
        "но не на каждое сообщение — раз в несколько сообщений. Если тегнёшь меня, "
        "ответишь на моё сообщение или явно задашь вопрос — отвечу почти всегда."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    lines = [f"/{c.command} — {c.description}" for c in BOT_COMMANDS]
    await message.reply("Команды:\n" + "\n".join(lines))


@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    await message.reply("pong")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    chat_id = message.chat.id
    await message.reply(
        "Настройки:\n"
        f"— комментирую раз в {MIN_MESSAGES_BETWEEN_COMMENTS}-{MAX_MESSAGES_BETWEEN_COMMENTS} "
        "сообщений (случайно каждый раз)\n"
        f"— окно контекста: до {CONTEXT_WINDOW} сообщений / ~{MAX_INPUT_TOKENS} токенов\n"
        f"— до следующей плановой проверки в этом чате: "
        f"{max(0, get_threshold(chat_id) - messages_since_check[chat_id])} сообщений\n"
        "— на упоминание, ответ мне или явный вопрос реагирую почти всегда"
    )


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def on_group_message(message: Message):
    chat_id = message.chat.id
    name = message.from_user.full_name if message.from_user else "unknown"
    history[chat_id].append((name, message.text))
    messages_since_check[chat_id] += 1

    bot_username = (await bot.get_me()).username
    mentioned = bool(bot_username) and f"@{bot_username}".lower() in message.text.lower()
    is_reply_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.username == bot_username
    )
    is_question = looks_like_question(message.text)
    forced = mentioned or is_reply_to_bot

    threshold_reached = messages_since_check[chat_id] >= get_threshold(chat_id)
    if not (forced or is_question or threshold_reached):
        return

    now = time.monotonic()
    if not forced and (now - last_comment_at[chat_id] < MIN_SECONDS_BETWEEN_COMMENTS):
        return

    trigger_reason = "mention_or_reply" if forced else ("question" if is_question else "scheduled")
    context_text = format_history(chat_id)

    try:
        comment = await decide_and_comment(
            BOT_PROMPT, context_text, forced=forced, trigger_reason=trigger_reason
        )
    except Exception:
        log.exception("Failed to get a decision from the LLM")
        return

    # A check happened (regardless of the outcome) — restart the countdown to the next one.
    reset_threshold(chat_id)

    if not comment:
        return

    last_comment_at[chat_id] = now
    await message.reply(comment)


async def main():
    log.info("Starting Backseat bot")
    await bot.set_my_commands(BOT_COMMANDS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
