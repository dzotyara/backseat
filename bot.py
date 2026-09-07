import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message, ReplyParameters

from llm_client import decide, estimate_tokens

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backseat")


def _env(name: str, default: str) -> str:
    """Like os.environ.get, but treats an empty string as 'not set' too
    (env_file entries like FOO= otherwise override real defaults with '')."""
    value = os.environ.get(name)
    return value if value else default


TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

_BOT_PROMPT_FILE = _env("BOT_PROMPT_FILE", "prompt.txt")
_BOT_PROMPT_INLINE_DEFAULT = "Комментируй сообщения участников чата коротко и едко."
if _BOT_PROMPT_FILE and os.path.isfile(_BOT_PROMPT_FILE):
    with open(_BOT_PROMPT_FILE, encoding="utf-8") as f:
        BOT_PROMPT = f.read().strip()
    log.info("Loaded BOT_PROMPT from %s", _BOT_PROMPT_FILE)
else:
    BOT_PROMPT = _env("BOT_PROMPT", _BOT_PROMPT_INLINE_DEFAULT)
    log.info("BOT_PROMPT_FILE not found, using inline BOT_PROMPT env var")

# --- Batching / debounce ---
CONTEXT_WINDOW = int(_env("CONTEXT_WINDOW", "25"))
DEBOUNCE_SECONDS = float(_env("DEBOUNCE_SECONDS", "5"))
MENTION_DEBOUNCE_SECONDS = float(_env("MENTION_DEBOUNCE_SECONDS", "2"))
MAX_BATCH_WAIT_SECONDS = float(_env("MAX_BATCH_WAIT_SECONDS", "15"))
MAX_BATCH_MESSAGES = int(_env("MAX_BATCH_MESSAGES", "15"))
MAX_CONTEXT_MESSAGE_CHARS = int(_env("MAX_CONTEXT_MESSAGE_CHARS", "500"))
MAX_NEW_MESSAGE_CHARS = int(_env("MAX_NEW_MESSAGE_CHARS", "1000"))
MAX_INPUT_TOKENS = int(_env("MAX_INPUT_TOKENS", "1500"))
# Cooldown counts from the last comment Backseat actually sent, not from decisions.
MIN_SECONDS_BETWEEN_COMMENTS = float(_env("MIN_SECONDS_BETWEEN_COMMENTS", "25"))
# Whether a direct mention/reply-to-bot skips the cooldown (batching still applies either way).
MENTION_BYPASSES_COOLDOWN = _env("MENTION_BYPASSES_COOLDOWN", "true").lower() == "true"

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

BOT_COMMANDS = [
    BotCommand(command="start", description="О боте и как он работает"),
    BotCommand(command="help", description="Список команд"),
    BotCommand(command="ping", description="Проверка, что бот жив"),
    BotCommand(command="status", description="Текущие настройки бота"),
]

BOT_USERNAME: str | None = None  # resolved once at startup


@dataclass
class ChatState:
    pending: list[dict] = field(default_factory=list)
    context: deque = field(default_factory=lambda: deque(maxlen=CONTEXT_WINDOW))
    batch_start: float | None = None
    debounce_task: asyncio.Task | None = None
    last_comment_at: float = 0.0
    process_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_chat_states: dict[int, ChatState] = {}


def get_state(chat_id: int) -> ChatState:
    if chat_id not in _chat_states:
        _chat_states[chat_id] = ChatState()
    return _chat_states[chat_id]


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def serialize_context(state: ChatState) -> str:
    lines = [
        f"{m['id']}|{m['author']}|{_truncate(m['text'], MAX_CONTEXT_MESSAGE_CHARS)}"
        for m in state.context
    ]
    while lines and estimate_tokens("\n".join(lines)) > MAX_INPUT_TOKENS:
        lines.pop(0)
    return "\n".join(lines)


def serialize_new(batch: list[dict]) -> str:
    """Compact 'id|author|text' lines, collapsing consecutive identical
    (author, text) repeats into 'id_start-id_end|author|text|xN'."""
    lines = []
    i = 0
    while i < len(batch):
        j = i
        while (
            j + 1 < len(batch)
            and batch[j + 1]["author"] == batch[i]["author"]
            and batch[j + 1]["text"] == batch[i]["text"]
        ):
            j += 1
        group = batch[i : j + 1]
        text = _truncate(group[0]["text"], MAX_NEW_MESSAGE_CHARS)
        if len(group) > 1:
            lines.append(f"{group[0]['id']}-{group[-1]['id']}|{group[0]['author']}|{text}|x{len(group)}")
        else:
            lines.append(f"{group[0]['id']}|{group[0]['author']}|{text}")
        i = j + 1
    return "\n".join(lines)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.reply(
        "Привет, я Backseat. Слежу за чатом и иногда вставляю комментарий по своему "
        "характеру — не на каждое сообщение, а когда есть повод. На упоминание, "
        "ответ мне или явный вопрос реагирую почти всегда."
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
    state = get_state(message.chat.id)
    now = time.monotonic()
    cooldown_left = max(0.0, MIN_SECONDS_BETWEEN_COMMENTS - (now - state.last_comment_at))
    await message.reply(
        "Настройки:\n"
        f"— debounce: {DEBOUNCE_SECONDS}с (упоминание/ответ: {MENTION_DEBOUNCE_SECONDS}с), "
        f"макс. ожидание пачки: {MAX_BATCH_WAIT_SECONDS}с, макс. размер пачки: {MAX_BATCH_MESSAGES}\n"
        f"— кулдаун между комментариями: {MIN_SECONDS_BETWEEN_COMMENTS}с "
        f"(осталось: {cooldown_left:.0f}с)\n"
        f"— контекст: до {CONTEXT_WINDOW} сообщений / ~{MAX_INPUT_TOKENS} токенов\n"
        f"— сейчас в необработанной пачке: {len(state.pending)} сообщений"
    )


def _schedule_batch(chat_id: int, delay: float) -> None:
    state = get_state(chat_id)
    if state.debounce_task and not state.debounce_task.done():
        state.debounce_task.cancel()
    state.debounce_task = asyncio.create_task(_debounce_then_process(chat_id, delay))


async def _debounce_then_process(chat_id: int, delay: float) -> None:
    try:
        if delay > 0:
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    await process_batch(chat_id)


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def on_group_message(message: Message):
    chat_id = message.chat.id
    state = get_state(chat_id)
    name = message.from_user.full_name if message.from_user else "unknown"

    mentioned = bool(BOT_USERNAME) and f"@{BOT_USERNAME}".lower() in message.text.lower()
    is_reply_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.username == BOT_USERNAME
    )
    forced = mentioned or is_reply_to_bot

    state.pending.append(
        {"id": message.message_id, "author": name, "text": message.text, "forced": forced}
    )
    if state.batch_start is None:
        state.batch_start = time.monotonic()

    any_forced = any(m["forced"] for m in state.pending)
    base_delay = MENTION_DEBOUNCE_SECONDS if any_forced else DEBOUNCE_SECONDS
    elapsed = time.monotonic() - state.batch_start
    remaining_wait = MAX_BATCH_WAIT_SECONDS - elapsed
    delay = max(0.0, min(base_delay, remaining_wait))

    if len(state.pending) >= MAX_BATCH_MESSAGES:
        delay = 0.0

    _schedule_batch(chat_id, delay)


async def process_batch(chat_id: int) -> None:
    state = get_state(chat_id)
    async with state.process_lock:
        if not state.pending:
            return

        # Atomically detach the current batch; anything arriving during the
        # upcoming LLM call starts a fresh pending list / batch_start.
        batch = state.pending
        state.pending = []
        state.batch_start = None

        forced = any(m["forced"] for m in batch)
        now = time.monotonic()
        cooldown_active = (now - state.last_comment_at) < MIN_SECONDS_BETWEEN_COMMENTS
        skip_for_cooldown = cooldown_active and not (forced and MENTION_BYPASSES_COOLDOWN)

        if skip_for_cooldown:
            state.context.extend(batch)
            log.info("chat_id=%s decision=skip cooldown_skip=true batch_size=%d", chat_id, len(batch))
            return

        context_text = serialize_context(state)
        new_text = serialize_new(batch)

        try:
            result = await decide(BOT_PROMPT, context_text, new_text, forced=forced)
        except Exception:
            log.exception("chat_id=%s LLM call failed, batch_size=%d", chat_id, len(batch))
            state.context.extend(batch)
            return

        state.context.extend(batch)

        if not result or not result.get("respond"):
            log.info("chat_id=%s decision=skip batch_size=%d", chat_id, len(batch))
            return

        valid_ids = {m["id"] for m in batch}
        reply_id = result.get("reply_to_message_id")
        if reply_id not in valid_ids:
            reply_id = batch[-1]["id"]

        comment = result.get("comment")
        if not comment:
            log.info("chat_id=%s decision=skip (empty comment) batch_size=%d", chat_id, len(batch))
            return

        try:
            await bot.send_message(
                chat_id, comment, reply_parameters=ReplyParameters(message_id=reply_id)
            )
        except Exception:
            log.warning(
                "chat_id=%s failed to reply to message_id=%s (maybe deleted), sending without reply",
                chat_id, reply_id,
            )
            try:
                await bot.send_message(chat_id, comment)
            except Exception:
                log.exception("chat_id=%s failed to send comment at all", chat_id)
                return

        state.last_comment_at = now
        log.info("chat_id=%s decision=reply reply_to=%s batch_size=%d", chat_id, reply_id, len(batch))


async def main():
    global BOT_USERNAME
    log.info("Starting Backseat bot")
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await bot.set_my_commands(BOT_COMMANDS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
